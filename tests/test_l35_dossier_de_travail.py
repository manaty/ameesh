# SPDX-License-Identifier: AGPL-3.0-only
"""L35 — dossier de travail après la bascule (régression de L31, 0029).

1. Repli transitoire sur le `cwd` d'une ancienne fiche Placement quand la
   politique de l'hôte ne donne rien, avec un diagnostic (canon check, canon
   sync) ; la politique de l'hôte reste prioritaire.
2. Un dossier par agent : `policy.work_dirs` et le gabarit `{agent}`, validés.
3. Dossier absent : plus de tentative ni de journal à chaque sondage ; attente
   croissante bornée, reprise dès que la ligne du registre change, statut levé
   même sans travail en attente.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import canon, registry
from ameesh.runner import AgentWorker, Runner

from . import test_canon
from .support import FAKEBIN, PgTestCase
from .test_canon import write

BANC = """---
type: Host
title: banc
responsible: human:alice
policy:
  harnesses: [deepseek, codex]
  providers: [deepseek, openai]
  credential_modes: [api-key]
  max_agents: 2
%s
---

# banc
"""

ADMISSION = """---
type: Placement
title: ouvrier@banc
agent: ouvrier
hosts: [banc]
credential_mode: api-key
%s
---
"""


class _Canon(test_canon._TmpMixin):
    def host(self, work: str = "") -> None:
        write(self.root, "hotes/banc.md", BANC % work)

    def admission(self, extra: str = "") -> None:
        write(self.root, "placements/ouvrier-banc.md", ADMISSION % extra)

    def findings(self, code: str) -> list:
        return [f for f in canon.validate(canon.load(self.root, untrusted=True))
                if f.code == code]


class PolitiqueTest(_Canon, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def test_work_dirs_prioritaire(self):
        policy = canon.HostPolicy(work_root="/srv", work_roots={"acme-web": "/x/acme"},
                                  work_dirs={"ouvrier": "/wt/ouvrier"})
        self.assertEqual(policy.work_dir("acme-web", "ouvrier"), "/wt/ouvrier")
        self.assertEqual(policy.work_dir("acme-web", "autre"), "/x/acme")
        self.assertEqual(policy.work_dir("autre-projet", "autre"), "/srv/autre-projet")

    def test_gabarit_agent(self):
        policy = canon.HostPolicy(work_roots={"nexlink": "~/dev/nexlink-{agent}"},
                                  work_root="/wt/{agent}")
        self.assertEqual(policy.work_dir("nexlink", "codex2"), "~/dev/nexlink-codex2")
        self.assertEqual(policy.work_dir("acme", "codex2"), "/wt/codex2/acme")
        # sans agent, un gabarit ne devient jamais un chemin littéral
        self.assertIsNone(policy.work_dir("nexlink"))
        self.assertIsNone(policy.work_dir("acme"))

    def test_canon_lit_work_dirs_et_gabarit(self):
        self.host("  work_dirs: {ouvrier: /wt/perso}\n"
                  "  work_roots: {acme-web: '/srv/acme-{agent}'}")
        loaded = canon.load(self.root, untrusted=True)
        host = loaded.host("banc")
        self.assertEqual(host.policy.work_dirs, {"ouvrier": "/wt/perso"})
        self.assertEqual(host.policy.work_dir("acme-web", "relecteur"), "/srv/acme-relecteur")
        self.assertEqual(self.findings("host-policy-invalid"), [])

    def test_validation_gabarit_inconnu_et_chemin_vide(self):
        for work, attendu in (
                ("  work_roots: {acme-web: '/srv/{projet}'}", "gabarit inconnu"),
                ("  work_dirs: {ouvrier: '/srv/{agnet}'}", "gabarit inconnu"),
                ("  work_root: '/srv/{agent'", "accolade orpheline"),
                ("  work_root: ''", "chemin vide"),
                ("  work_dirs: {ouvrier: ''}", "chemin vide"),
                ("  work_dirs: [ouvrier]", "mapping"),
        ):
            with self.subTest(work=work):
                self.host(work)
                trouves = self.findings("host-policy-invalid")
                self.assertTrue(trouves, work)
                self.assertTrue(all(f.severity == canon.ERROR for f in trouves))
                self.assertIn(attendu, " ".join(f.message for f in trouves))

    def test_work_dirs_agent_inconnu_averti(self):
        self.host("  work_dirs: {fantome: /wt/x}\n  work_roots: {acme-web: /srv/a}")
        self.assertTrue(self.findings("host-work-dir-agent-unknown"))

    def test_cwd_herite_quand_l_hote_ne_donne_rien(self):
        self.host("")
        self.admission("cwd: ~/wt/ouvrier")
        herites = self.findings("admission-cwd-inherited")
        self.assertEqual(len(herites), 1)
        self.assertEqual(herites[0].severity, canon.WARNING)
        self.assertIn("cwd hérité de la fiche Placement", herites[0].message)
        self.assertIn("work_dirs", herites[0].message)
        self.assertEqual(self.findings("admission-cwd-ignored"), [])

    def test_cwd_ignore_quand_l_hote_donne_un_dossier(self):
        self.host("  work_dirs: {ouvrier: /wt/ouvrier}")
        self.admission("cwd: ~/ancien")
        self.assertTrue(self.findings("admission-cwd-ignored"))
        self.assertEqual(self.findings("admission-cwd-inherited"), [])

    def test_aucun_dossier_averti(self):
        self.host("")
        self.admission("")
        manque = self.findings("host-work-dir-missing")
        self.assertEqual([f.agent for f in manque], ["ouvrier"])


class SyncTest(_Canon, test_canon._CanonDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM visibility_checks")

    def action(self, report):
        return {a.agent: a for a in report.actions}["ouvrier"]

    def test_repli_placement_cwd_et_note(self):
        self.host("")
        self.admission("cwd: ~/wt/ouvrier")
        action = self.action(self.sync("banc"))
        self.assertEqual(self.row("ouvrier")["cwd"], os.path.expanduser("~/wt/ouvrier"))
        self.assertIn("cwd hérité de la fiche Placement", action.detail)
        self.assertIn("work_dirs", action.detail)

    def test_politique_prioritaire_sur_placement(self):
        self.host("  work_roots: {acme-web: /srv/acme/acme-web}")
        self.admission("cwd: ~/wt/ouvrier")
        action = self.action(self.sync("banc"))
        self.assertEqual(self.row("ouvrier")["cwd"], "/srv/acme/acme-web")
        self.assertNotIn("hérité", action.detail)

    def test_work_dirs_et_gabarit_dans_le_registre(self):
        self.host("  work_dirs: {ouvrier: ~/wt/perso}")
        self.sync("banc")
        self.assertEqual(self.row("ouvrier")["cwd"], os.path.expanduser("~/wt/perso"))
        self.host("  work_roots: {acme-web: '/srv/nexlink-{agent}'}")
        self.assertEqual(self.action(self.sync("banc")).action, "mis à jour")
        self.assertEqual(self.row("ouvrier")["cwd"], "/srv/nexlink-ouvrier")

    def test_aucun_dossier_note(self):
        self.host("")
        action = self.action(self.sync("banc"))
        self.assertIsNone(self.row("ouvrier")["cwd"])
        self.assertIn("aucun dossier de travail", action.detail)


class BoucleTest(PgTestCase):
    """L'exécuteur face à un dossier absent : journal, attente, reprise."""

    def setUp(self) -> None:
        super().setUp()
        self.logs: list[str] = []
        patcher = mock.patch("ameesh.runner.log", side_effect=self.logs.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                           "AMEESH_TEST_LOG": self.turns_log})
        env.start()
        self.addCleanup(env.stop)

    def worker(self, name: str) -> AgentWorker:
        self.cwd = os.path.join(self.tmp, "work", name)
        self.register(name, "claude", cwd=os.path.join(self.tmp, "absent", name))
        runner = Runner(self.cfg, self.db, once=True)
        runner.budget_usd_per_hour = 0
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def absent_logs(self) -> list[str]:
        return [m for m in self.logs if "dossier de travail absent" in m]

    def tour(self, worker):
        spec = worker.pick()
        if spec is not None:
            worker.run_turn(spec)
        return spec

    def test_journal_unique_attente_croissante_et_reprise(self):
        w = self.worker("l35-boucle")
        registry.set_pending_prompt(self.db, w.name, "travail")
        self.assertIsNotNone(self.tour(w))          # le tour tente, le dossier manque
        row = registry.get(self.db, w.name)
        self.assertEqual((row["status"], row["status_text"]), ("blocked", "dossier absent"))
        self.assertEqual(row["pending_prompt"], "travail")   # consigne rendue
        self.assertEqual(len(self.absent_logs()), 1)
        self.assertEqual(w._wd_delay, w.WORKDIR_BACKOFF_MIN)
        # sondages suivants : rien n'est consommé, pas de nouvelle ligne
        with mock.patch.object(w, "adopt_moved_worktree", return_value=None) as adopt:
            for _ in range(5):
                self.assertIsNone(self.tour(w))
            self.assertEqual(adopt.call_count, 0)    # échéance non atteinte
            delais = []
            for _ in range(8):
                w._wd_next = 0.0                     # échéance atteinte
                self.assertIsNone(self.tour(w))
                delais.append(w._wd_delay)
            self.assertEqual(adopt.call_count, 8)
        self.assertEqual(delais[:4], [10.0, 20.0, 40.0, 80.0])
        self.assertEqual(max(delais), w.WORKDIR_BACKOFF_MAX)
        self.assertEqual(len(self.absent_logs()), 1)
        self.assertEqual(registry.get(self.db, w.name)["pending_prompt"], "travail")
        # canon sync corrige le cwd : reprise au sondage suivant, sans attendre
        os.makedirs(self.cwd, exist_ok=True)
        registry.upsert(self.db, w.name, cwd=self.cwd)
        spec = w.pick()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "prompt")
        self.assertTrue(any("retrouvé" in m for m in self.logs))
        self.assertFalse(w._wd_blocked)

    def test_statut_leve_sans_travail_en_attente(self):
        w = self.worker("l35-repos")
        registry.set_pending_prompt(self.db, w.name, "travail")
        self.tour(w)
        registry.set_pending_prompt(self.db, w.name, None)
        w.nudged = True                                      # pas de relance de repos
        os.makedirs(self.cwd, exist_ok=True)
        registry.upsert(self.db, w.name, cwd=self.cwd)
        self.assertIsNone(w.pick())                          # rien à faire…
        row = registry.get(self.db, w.name)
        self.assertEqual(row["status"], "idle")              # … mais plus bloqué
        self.assertIsNone(row["last_error"])

    def test_dossier_recree_sans_changement_du_registre(self):
        w = self.worker("l35-recree")
        registry.set_pending_prompt(self.db, w.name, "travail")
        self.tour(w)
        os.makedirs(registry.get(self.db, w.name)["cwd"], exist_ok=True)
        self.assertIsNotNone(w.pick())                       # contrôle gratuit à chaque sondage

    def test_blocage_herite_d_un_executeur_precedent(self):
        w = self.worker("l35-herite")
        registry.set_pending_prompt(self.db, w.name, "travail")
        self.tour(w)
        registry.set_pending_prompt(self.db, w.name, None)
        self.logs.clear()
        w.release_lease()
        # nouvel exécuteur (redémarrage) : il reprend le blocage sans le rejournaliser
        runner = Runner(self.cfg, self.db, once=True)
        runner.budget_usd_per_hour = 0
        lease = registry.claim(self.db, w.name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        w2 = AgentWorker(runner, registry.get(self.db, w.name), lease)
        self.addCleanup(w2.watchdog_stop.set)
        w2.nudged = True
        self.assertTrue(w2._wd_blocked)
        self.assertIsNone(w2.pick())
        self.assertEqual(self.absent_logs(), [])
        os.makedirs(registry.get(self.db, w.name)["cwd"], exist_ok=True)
        self.assertIsNone(w2.pick())
        self.assertEqual(registry.get(self.db, w.name)["status"], "idle")

    # -- concurrence (relecture codex2 R1, B1) : transitions fencées par le bail --
    def bloque(self, name: str) -> AgentWorker:
        w = self.worker(name)
        registry.set_pending_prompt(self.db, w.name, "travail")
        self.tour(w)
        self.assertTrue(w._wd_blocked)
        os.makedirs(self.cwd, exist_ok=True)
        registry.upsert(self.db, w.name, cwd=self.cwd)       # le dossier revient
        return w

    def assert_rien_consomme(self, w: AgentWorker) -> None:
        row = registry.get(self.db, w.name)
        self.assertEqual(row["pending_prompt"], "travail")
        self.assertIsNone(row["current_prompt"])

    def test_levee_refusee_bail_remplace(self):
        w = self.bloque("l35-remplace")
        self.db.execute("UPDATE agent_registry SET lease_owner = 'autre-executeur', "
                        "lease_epoch = lease_epoch + 1 WHERE name = %s", (w.name,))
        self.assertIsNone(w.pick())
        row = registry.get(self.db, w.name)
        self.assertEqual((row["status"], row["status_text"]), ("blocked", "dossier absent"))
        self.assert_rien_consomme(w)
        self.assertTrue(w._wd_blocked)                       # rien de confirmé localement
        self.assertTrue(w.lease_lost.is_set())
        self.assertFalse(any("— reprise" in m for m in self.logs))

    def test_levee_refusee_bail_echu(self):
        w = self.bloque("l35-echu")
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - interval '1 second'"
                        " WHERE name = %s", (w.name,))
        self.assertIsNone(w.pick())
        self.assertEqual(registry.get(self.db, w.name)["status"], "blocked")
        self.assert_rien_consomme(w)
        self.assertTrue(w._wd_blocked)

    def test_pose_refusee_bail_perdu(self):
        w = self.worker("l35-pose-bail")
        self.db.execute("UPDATE agent_registry SET lease_owner = 'autre-executeur' "
                        "WHERE name = %s", (w.name,))
        w.agent = registry.get(self.db, w.name)
        self.assertFalse(w._workdir_missing())
        row = registry.get(self.db, w.name)
        self.assertNotEqual(row["status"], "blocked")
        self.assertFalse(w._wd_blocked)
        self.assertTrue(w.lease_lost.is_set())

    def test_pose_preserve_arret_et_autre_blocage(self):
        w = self.worker("l35-pose-statut")
        for statut, texte in (("stopped", "arrêté à la main"),
                              ("blocked", "budget : plafond")):
            with self.subTest(statut=statut):
                registry.set_status(self.db, w.name, statut, status_text=texte)
                w.agent = registry.get(self.db, w.name)
                w._wd_blocked = False
                self.assertEqual(registry.set_marked_block(
                    self.db, w.name, w.runner.runner_id, w.epoch, w.WORKDIR_STATUS,
                    "dossier de travail absent : x", w.WORKDIR_ERROR), "kept")
                self.assertTrue(w._workdir_missing(force_status=True))
                row = registry.get(self.db, w.name)
                self.assertEqual((row["status"], row["status_text"]), (statut, texte))

    def test_levee_preserve_arret_et_autre_blocage(self):
        w = self.bloque("l35-levee-statut")
        for statut, texte in (("blocked", "budget : plafond"),
                              ("stopped", "arrêté à la main")):
            with self.subTest(statut=statut):
                registry.set_status(self.db, w.name, statut, status_text=texte,
                                    error="dossier de travail absent : x")
                self.assertEqual(registry.clear_marked_block(
                    self.db, w.name, w.runner.runner_id, w.epoch, w.WORKDIR_STATUS,
                    w.WORKDIR_ERROR), "kept")
                row = registry.get(self.db, w.name)
                self.assertEqual((row["status"], row["status_text"]), (statut, texte))

    def test_pick_arrete_pendant_le_blocage_ne_consomme_rien(self):
        w = self.bloque("l35-arrete")
        registry.set_status(self.db, w.name, "stopped", status_text="arrêté à la main")
        self.assertIsNone(w.pick())
        self.assertTrue(w.stopping.is_set())
        self.assertEqual(registry.get(self.db, w.name)["status"], "stopped")
        self.assert_rien_consomme(w)

    def test_courrier_non_consomme_pendant_le_blocage(self):
        from ameesh import mail
        w = self.worker("l35-courrier")
        registry.set_pending_prompt(self.db, w.name, "travail")
        self.tour(w)
        mail.send(self.db, "collegue", w.name, "bonjour")
        avant = len(mail.unread(self.db, w.name))
        self.assertEqual(avant, 1)
        self.assertIsNone(w.pick())
        self.assertEqual(len(mail.unread(self.db, w.name)), avant)
        self.assert_rien_consomme(w)

    # -- course lecture → levée (relecture codex2 R2) ----------------------------
    def course(self, name: str, statut: str, texte: str) -> AgentWorker:
        """Statut concurrent écrit APRÈS la lecture du registre par `pick()` et
        AVANT la levée ; un courrier attend aussi."""
        from ameesh import mail
        w = self.bloque(name)
        mail.send(self.db, "collegue", w.name, "bonjour")
        vrai = registry.clear_marked_block

        def injecte(*args, **kwargs):
            registry.set_status(self.db, w.name, statut, status_text=texte)
            return vrai(*args, **kwargs)

        with mock.patch("ameesh.registry.clear_marked_block", side_effect=injecte):
            self.assertIsNone(w.pick())
        row = registry.get(self.db, w.name)
        self.assertEqual((row["status"], row["status_text"]), (statut, texte))
        self.assert_rien_consomme(w)
        self.assertEqual(len(mail.unread(self.db, w.name)), 1)
        self.assertFalse(any("— reprise" in m for m in self.logs))
        self.assertFalse(w._wd_blocked)              # calé sur le statut conservé
        return w

    def test_course_arret_entre_lecture_et_levee(self):
        w = self.course("l35-course-stop", "stopped", "arrêté à la main")
        self.assertIsNone(w.pick())                  # sondage suivant : l'arrêt est lu
        self.assertTrue(w.stopping.is_set())
        self.assertEqual(registry.get(self.db, w.name)["status"], "stopped")
        self.assert_rien_consomme(w)

    def test_course_autre_blocage_entre_lecture_et_levee(self):
        self.course("l35-course-budget", "blocked", "budget : plafond")

    def test_consommation_refusee_sur_agent_arrete(self):
        w = self.worker("l35-defense")
        registry.set_pending_prompt(self.db, w.name, "travail")
        registry.set_status(self.db, w.name, "stopped", status_text="arrêté à la main")
        self.assertIsNone(registry.take_pending_prompt(
            self.db, w.name, w.runner.runner_id, w.epoch))
        self.assertFalse(registry.begin_turn(
            self.db, w.name, w.runner.runner_id, w.epoch, "tour"))
        row = registry.get(self.db, w.name)
        self.assertEqual((row["status"], row["pending_prompt"]), ("stopped", "travail"))


if __name__ == "__main__":
    unittest.main()
