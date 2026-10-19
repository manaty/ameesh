# SPDX-License-Identifier: AGPL-3.0-only
"""Exécuteur : tours, reprise de session, baux, expiration, réveil sur NOTIFY."""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from ameesh import adapters, db as db_mod, mail, registry
from ameesh import platform as os_layer
from ameesh.runner import AgentWorker, Runner, _AsyncLog

from .support import FAKEBIN, PgTestCase

HARNESSES = {
    "claude": {"session": "claude-sess-1", "reprise": ["--resume", "claude-sess-1"]},
    "codex": {"session": "codex-thread-1", "reprise": ["resume", "codex-thread-1"]},
    "deepseek": {"session": "dsh-session-1", "reprise": ["--session-id", "dsh-session-1"]},
}

#: #2497 : motifs du journal par lesquels le battement ou le veilleur arrêtent
#: un harnais dont le bail est perdu (`AgentWorker.renew`,
#: `AgentWorker._deadline_watchdog`) ; `RaisonsDArretDeBailTest` les tient à jour.
ARRETS_DE_BAIL = ("lease lost", "lease deadline reached",
                  "renewal unanswered past the lease", "database unreachable")

#: #2502 : lignes que `stop_group_now` écrit *quand il arrête* le harnais. Un
#: diagnostic non définitif de `renew()` (« database unreachable … the turn
#: continues ») porte un motif d'arrêt sans qu'aucun arrêt n'ait eu lieu : le
#: témoin exige donc l'une de ces lignes en plus du motif.
LIGNES_ARRET = ("harness stopped (", "stop requested (")


def arret_de_bail(ligne: str) -> bool:
    """#2502 : la ligne prouve-t-elle un arrêt de bail (motif + ligne d'arrêt) ?

    « database unreachable » seul ne prouve rien : `renew()` le journalise
    aussi quand le bail court encore et que le tour continue.
    """
    return (any(p in ligne for p in LIGNES_ARRET)
            and any(m in ligne for m in ARRETS_DE_BAIL))


class HorlogeSimulee:
    """#2600 : horloge de bail simulée, sans dépendance à l'ordonnanceur.

    `time()` date l'échéance en base, `monotonic()` la borne locale du bail ;
    les deux avancent ensemble, uniquement quand le test le décide (le plus
    souvent parce qu'une attente simulée consomme son délai). Aucun test de
    bail, de battement ou de veilleur n'a besoin d'un bail réel de quelques
    secondes contre l'horloge d'une CI chargée.
    """

    def __init__(self, maintenant: float | None = None, limite: int = 1000):
        self.maintenant = time.time() if maintenant is None else float(maintenant)
        self.monotone = self.maintenant
        self.attentes: list[float | None] = []
        self.limite = limite

    def time(self) -> float:
        return self.maintenant

    def monotonic(self) -> float:
        return self.monotone

    def avance(self, secondes: float) -> None:
        secondes = max(0.0, float(secondes))
        self.maintenant += secondes
        self.monotone += secondes

    def consomme(self, timeout: float | None) -> None:
        """Compte et consomme une attente ; garde-fou contre une boucle à vide."""
        self.attentes.append(timeout)
        if len(self.attentes) > self.limite:
            raise AssertionError(
                "garde-fou #2600 : %d attentes simulées sans progrès"
                % self.limite)
        if timeout:
            self.avance(timeout)


class EvenementSimule:
    """Événement de réveil dont `wait` consomme le délai sur l'horloge simulée.

    Remplace l'`Event` réel d'un battement ou d'un veilleur : `wait` ne dort
    pas, avance l'horloge du délai demandé, puis laisse un callback décider de
    l'état (renouvellement, arrêt). C'est ce qui rend le témoin déterministe.
    """

    def __init__(self, horloge: HorlogeSimulee, apres=None):
        self.horloge = horloge
        self.apres = apres
        self.attentes: list[float | None] = []
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def clear(self) -> None:
        self._set = False

    def wait(self, timeout: float | None = None) -> bool:
        self.attentes.append(timeout)
        self.horloge.consomme(timeout)
        if self.apres is not None:
            self.apres(self.horloge)
        return self._set


class RaisonsDArretDeBailTest(unittest.TestCase):
    def test_chaque_arret_de_bail_est_attendu_par_le_temoin(self):
        """#2497 : `test_bail_perdu_interrompt_le_tour` attendait deux raisons
        sur quatre ; sous charge, « renewal unanswered past the lease » arrêtait
        bien le harnais mais le témoin restait rouge. Toute raison d'arrêt de
        ces deux chemins doit figurer dans `ARRETS_DE_BAIL`."""
        import inspect
        import re
        raisons = []
        for fn in (AgentWorker.renew, AgentWorker._deadline_watchdog):
            raisons += re.findall(r'stop_group_now\(\s*"([^"]+)"', inspect.getsource(fn))
        self.assertGreaterEqual(len(raisons), 5)
        for raison in raisons:
            self.assertTrue(any(m in raison for m in ARRETS_DE_BAIL), raison)

    def test_diagnostic_non_definitif_ne_vaut_pas_un_arret(self):
        """#2502 : le diagnostic « database unreachable … the turn continues »
        de `renew()` n'arrête pas le harnais ; le témoin ne doit pas le prendre
        pour une preuve d'arrêt. Seules les lignes de `stop_group_now` en sont
        une."""
        diagnostic = ("[vole] heartbeat: database unreachable (connection "
                      "timeout exceeded) — failure 1, lease still valid for 2s: "
                      "the turn continues, retrying soon")
        # le diagnostic porte bien un motif d'arrêt : c'est le piège que le
        # resserrement doit écarter (sinon ce témoin ne prouverait rien)
        self.assertTrue(any(m in diagnostic for m in ARRETS_DE_BAIL))
        self.assertFalse(arret_de_bail(diagnostic))
        self.assertTrue(arret_de_bail(
            "[vole] harness stopped (database unreachable)"))
        self.assertTrue(arret_de_bail(
            "[vole] stop requested (lease lost): harness not yet published"))

    def test_arret_sans_motif_de_bail_ne_compte_pas(self):
        """#2502 : une ligne d'arrêt d'une autre cause (budget, préemption,
        redémarrage) ne prouve pas la perte du bail."""
        self.assertFalse(arret_de_bail("[vole] harness stopped (budget guard)"))


class RunnerTest(PgTestCase):
    def _cwd(self, name: str) -> str:
        path = os.path.join(self.tmp, "work", name)
        os.makedirs(path, exist_ok=True)
        return path

    def _messages(self) -> list[str]:
        return [m["body"] for m in mail.unread(self.db, "orchestrateur")]

    # -- tours et sessions -------------------------------------------------
    def test_un_tour_par_harnais_puis_reprise_de_session(self):
        for harness, attendu in HARNESSES.items():
            with self.subTest(harness=harness):
                name = "agent-" + harness
                cwd = self._cwd(name)
                self.assertEqual(
                    self.register(name, harness, cwd=cwd, prompt="premier tour").returncode, 0)
                proc = self.runner("--once", "--agents", name)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

                row = registry.get(self.db, name)
                self.assertEqual(row["session_id"], attendu["session"])
                self.assertEqual(int(row["turns"]), 1)
                self.assertEqual(row["status"], "idle")
                self.assertIn("instruction", row["status_text"])
                # l'événement brut du harnais est journalisé sur disque
                events = os.path.join(self.state, name, "events.jsonl")
                self.assertTrue(os.path.exists(events))
                with open(events, encoding="utf-8") as fh:
                    self.assertIn(attendu["session"], fh.read())

                # deuxième consigne : la session est reprise
                self.register(name, harness, prompt="deuxième tour")
                proc = self.runner("--once", "--agents", name)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                appels = [t for t in self.turns() if t["harness"] == harness]
                self.assertEqual(len(appels), 2, appels)
                self.assertEqual(appels[1]["argv"][-1], "deuxième tour")
                for marqueur in attendu["reprise"]:
                    self.assertIn(marqueur, appels[1]["argv"])
                self.assertEqual(int(registry.get(self.db, name)["turns"]), 2)

    def test_cout_et_session_enregistres(self):
        cwd = self._cwd("couteux")
        self.register("couteux", "claude", cwd=cwd, prompt="tour")
        self.runner("--once", "--agents", "couteux")
        row = registry.get(self.db, "couteux")
        self.assertAlmostEqual(float(row["spent_usd"]), 0.0125, places=4)

    def test_dry_run_n_execute_rien(self):
        cwd = self._cwd("sec")
        self.register("sec", "claude", cwd=cwd, prompt="tour")
        proc = self.runner("--once", "--dry-run", "--agents", "sec")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("dry-run", proc.stdout)
        self.assertEqual(self.turns(), [])
        row = registry.get(self.db, "sec")
        self.assertEqual(int(row["turns"]), 0)

    def test_harnais_absent_marque_bloque_sans_boucle(self):
        cwd = self._cwd("sansharnais")
        self.register("sansharnais", "claude", cwd=cwd, prompt="tour")
        env = self.env(AMEESH_CLAUDE_BIN=os.path.join(self.tmp, "claude-inexistant"))
        proc = self.runner("--once", "--agents", "sansharnais", env=env)
        self.assertEqual(proc.returncode, 0)
        row = registry.get(self.db, "sansharnais")
        self.assertEqual(row["status"], "blocked")
        self.assertIn("not found", row["last_error"])
        self.assertEqual(self.turns(), [])
        self.assertEqual(proc.stdout.count("not found"), 1)

    def test_consigne_restauree_apres_echec_au_demarrage(self):
        """Sonde codex3 (6) : un tour qui ne démarre pas ne perd pas sa consigne."""
        cwd = self._cwd("restaure")
        self.register("restaure", "claude", cwd=cwd, prompt="consigne à retrouver")
        env = self.env(AMEESH_CLAUDE_BIN=os.path.join(self.tmp, "claude-absent"))
        proc = self.runner("--once", "--agents", "restaure", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        row = registry.get(self.db, "restaure")
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["pending_prompt"], "consigne à retrouver")
        self.assertIsNone(row["current_prompt"])

        # le tour suivant repart avec la même consigne, sans intervention
        proc = self.runner("--once", "--agents", "restaure")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(self.turns()), 1)
        self.assertEqual(self.turns()[0]["argv"][-1], "consigne à retrouver")
        self.assertIsNone(registry.get(self.db, "restaure")["pending_prompt"])

    def test_battement_arrete_le_harnais_si_la_base_reste_injoignable(self):
        """Sonde codex3 (5) : un DbError ne doit pas laisser le harnais sans bail."""
        horloge = HorlogeSimulee()
        runner = Runner(self.cfg, self.db, once=True)
        runner.clock = horloge
        worker = AgentWorker(
            runner,
            {"name": "bravo", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() + 3600},
        )
        with mock.patch.object(registry, "renew", side_effect=db_mod.DbError("base absente")):
            self.assertTrue(worker.renew())    # 1er échec : sursis
            self.assertTrue(worker.renew())    # 2e échec : sursis
            self.assertFalse(worker.renew())   # 3e : bail considéré perdu
        self.assertTrue(worker.lease_lost.is_set())

    def test_battement_arrete_le_harnais_a_l_echeance_connue(self):
        """Même avec un seul échec, si l'échéance du bail est atteinte on arrête."""
        horloge = HorlogeSimulee()
        runner = Runner(self.cfg, self.db, once=True)
        runner.clock = horloge
        worker = AgentWorker(
            runner,
            {"name": "charlie", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() - 1},
        )
        with mock.patch.object(registry, "renew", side_effect=db_mod.DbError("base absente")):
            self.assertFalse(worker.renew())
        self.assertTrue(worker.lease_lost.is_set())

    def test_le_battement_couvre_la_preparation_du_tour(self):
        """#2544 : le battement de bail couvre la préparation du tour.

        Run 39158768160 (psql 6/16) : sous charge, la préparation d'un tour
        dépassait le bail de 3 s avant que le battement ne démarre ; le veilleur
        tuait le worker avant le `Popen` et le tour ne démarrait jamais. Le bail
        protège le tour entier : son renouvellement doit commencer avec la
        préparation, pas une fois le harnais publié.

        #2600 : horloge simulée et gelée. La préparation attend le premier
        renouvellement (preuve que le battement tourne avant elle), sans
        dormir plus que le bail ni laisser le veilleur juger une horloge
        réelle : le témoin ne dépend plus de la charge de la CI.
        """
        cwd = self._cwd("lent")
        self.register("lent", "claude", cwd=cwd, prompt="tour préparé lentement")
        runner = Runner(self.cfg, self.db, once=True)
        runner.lease_ttl = 3.0
        lease = registry.claim(self.db, "lent", runner.runner_id, 3.0)
        self.assertIsNotNone(lease)
        # Gelée, calée sur l'échéance rendue par la base : le veilleur ne peut
        # pas expirer, et le témoin ne dépend pas de l'écart d'horloges.
        horloge = HorlogeSimulee(float(lease["lease_expires_ts"]) - runner.lease_ttl)
        runner.clock = horloge
        worker = AgentWorker(runner, registry.get(self.db, "lent"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        worker.ensure_watchdog()
        renouvele = threading.Event()
        vrai_renew = AgentWorker.renew

        def renew_espion(self_):
            resultat = vrai_renew(self_)
            if resultat:
                renouvele.set()
            return resultat

        ouverture = AgentWorker._ouverture

        def lente(self_):
            # La préparation précède le Popen : le battement doit déjà avoir
            # renouvelé le bail, sinon l'échéance tomberait pendant ce bloc.
            self.assertTrue(
                renouvele.wait(10.0),
                "aucun renouvellement pendant la préparation du tour : le "
                "battement ne la couvre pas")
            return ouverture(self_)

        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                          "AMEESH_TEST_LOG": self.turns_log}), \
                mock.patch.object(AgentWorker, "renew", renew_espion), \
                mock.patch.object(AgentWorker, "_ouverture", lente):
            ok = worker.run_turn({"kind": "prompt", "prompt": "consigne", "ids": []})
        self.assertTrue(ok, "le tour n'a pas démarré : le bail n'a pas survécu "
                            "à la préparation")
        self.assertFalse(worker.lease_lost.is_set(),
                         "le veilleur a tué le worker pendant la préparation du tour")
        self.assertEqual(len(self.turns()), 1, "le harnais n'a pas été lancé")

    def test_dossier_absent_marque_bloque(self):
        self.register("sansdossier", "claude", cwd=os.path.join(self.tmp, "jamais-cree"),
                      prompt="tour")
        proc = self.runner("--once", "--agents", "sansdossier")
        self.assertEqual(proc.returncode, 0)
        row = registry.get(self.db, "sansdossier")
        self.assertEqual(row["status"], "blocked")
        self.assertIn("dossier", row["last_error"])

    # -- baux --------------------------------------------------------------
    def test_deux_executeurs_se_disputent_un_bail(self):
        """Deux processus concurrents : un seul tour, un 0 et un 3."""
        cwd = self._cwd("coureur")
        self.register("coureur", "claude", cwd=cwd, prompt="course")
        env = self.env(AMEESH_TEST_SLEEP="4")
        premier = self.runner_popen("--once", "--agents", "coureur", env=env)
        second = self.runner_popen("--once", "--agents", "coureur", env=env)
        sortie1, _ = premier.communicate(timeout=60)
        sortie2, _ = second.communicate(timeout=60)
        codes = sorted([premier.returncode, second.returncode])
        self.assertEqual(codes, [0, 3], "codes %r\n%s\n%s" % (codes, sortie1, sortie2))
        self.assertEqual(len(self.turns()), 1, "un seul tour doit avoir lieu")
        gagnant = [s for s in (sortie1, sortie2) if "lease acquired" in s]
        perdant = [s for s in (sortie1, sortie2) if "lease acquired" not in s]
        self.assertEqual(len(gagnant), 1)
        self.assertIn("no work available", perdant[0])
        self.assertEqual(int(registry.get(self.db, "coureur")["turns"]), 1)
        # le gagnant a rendu le bail en sortant
        self.assertIsNone(registry.get(self.db, "coureur")["lease_owner"])

    def test_bail_expire_repris_par_un_autre_executeur(self):
        cwd = self._cwd("tombe")
        self.register("tombe", "claude", cwd=cwd, prompt="après expiration")
        registry.upsert(self.db, "tombe", harness="claude", host=self.cfg.host, cwd=cwd)
        registry.claim(self.db, "tombe", "runner-mort", 1.0)
        time.sleep(1.3)  # l'exécuteur est mort, son bail a expiré
        proc = self.runner("--once", "--agents", "tombe")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        row = registry.get(self.db, "tombe")
        self.assertGreaterEqual(int(row["lease_epoch"]), 2)
        self.assertEqual(int(row["turns"]), 1)
        self.assertEqual(len(self.turns()), 1)

    def test_bail_vivant_n_est_pas_vole(self):
        cwd = self._cwd("vivant")
        self.register("vivant", "claude", cwd=cwd, prompt="pas touche")
        registry.upsert(self.db, "vivant", harness="claude", host=self.cfg.host, cwd=cwd)
        registry.claim(self.db, "vivant", "runner-actif", 300)
        proc = self.runner("--once", "--agents", "vivant")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(self.turns(), [])
        self.assertEqual(registry.get(self.db, "vivant")["lease_owner"], "runner-actif")

    def test_bail_perdu_interrompt_le_tour(self):
        """Si le bail est volé, le harnais est terminé et le tour n'est pas compté.

        #2600 : le harnais dort 60 s et les attentes sont larges (30 s) au lieu
        d'une mesure de 7 s contre l'ordonnanceur. Le bail de bout en bout vaut
        20 s, pas 3 s : la préparation et le choix du tour ne doivent plus
        courir contre l'horloge sous charge (la cadence courte est couverte par
        les témoins à horloge simulée). Un battement qui ne verrait pas le vol
        laisserait le tour aller au bout : le témoin resterait rouge par
        `turns == 0`, sans faux rouge de charge.
        """
        cwd = self._cwd("vole")
        self.register("vole", "claude", cwd=cwd, prompt="long tour")
        env = self.env(AMEESH_TEST_SLEEP="60", AMEESH_LEASE_TTL="20")
        proc = self.runner_popen("--agents", "vole", "--lease-ttl", "20", "--poll", "2", env=env)
        lignes: list[str] = []
        lecteur = threading.Thread(
            target=lambda: lignes.extend(proc.stdout or ()), daemon=True)
        lecteur.start()
        try:
            self.wait_for(lambda: len(self.turns()) == 1, timeout=30)  # le tour a démarré
            self.db.execute(
                "UPDATE agent_registry SET lease_owner = 'voleur', "
                "lease_epoch = lease_epoch + 1, lease_expires_at = now() + interval '60 seconds', "
                "status = 'running' WHERE name = 'vole'"
            )
            # Toute issue de perte de bail est juste : le renouvellement voit
            # l'epoch volé (« bail perdu »), l'échéance est atteinte avant
            # l'appel, ou — sous charge, avec le battement à ttl/3 de #2483 —
            # l'appel ne répond pas avant l'échéance (#2497). Dans tous les cas
            # le harnais est arrêté et le tour n'est pas compté. #2502 : la
            # ligne doit prouver l'arrêt (`stop_group_now`), pas seulement
            # porter un motif — `renew()` journalise « database unreachable »
            # sans arrêter quand le bail court encore.
            self.wait_for(
                lambda: any(arret_de_bail(l) for l in lignes),
                timeout=30, msg=lambda: "".join(lignes))
        finally:
            proc.terminate()
            proc.wait(timeout=30)
            lecteur.join(timeout=10)
        # Arrêt propre, jamais une mort par signal : un fil démon encore dans un
        # `print` à la finalisation faisait abandonner CPython (SIGABRT, core).
        self.assertGreaterEqual(proc.returncode, 0, "code %r\n%s" % (proc.returncode, "".join(lignes)))
        self.assertNotIn("exiting immediately", "".join(lignes))  # arrêt propre, sans repli
        # end_turn est fencé : le tour volé n'est pas compté
        self.assertEqual(int(registry.get(self.db, "vole")["turns"]), 0)

    # -- arrêt borné (relecture codex2 de 669a6fe) --------------------------
    def _lire_jusqu_a(self, fd: int, motif: str, timeout: float = 20.0) -> str:
        import select
        lu = b""
        fin = time.monotonic() + timeout
        while motif.encode() not in lu:
            reste = fin - time.monotonic()
            self.assertGreater(reste, 0, "motif %r absent :\n%s" % (motif, lu.decode(errors="replace")))
            if select.select([fd], [], [], min(reste, 0.5))[0]:
                lu += os.read(fd, 65536)
        return lu.decode(errors="replace")

    @staticmethod
    def _remplir_tube(w: int) -> None:
        """Remplit le tube sans toucher au mode du descripteur partagé avec le
        fils (L63 : sans /proc, donc sur tous les OS POSIX). Un fil écrit un
        octet à la fois en mode bloquant ; le tube est plein quand le fil
        reste bloqué. Il écrit sur sa propre copie du descripteur et se
        termine (tube cassé) quand le test ferme `r`."""
        ecrits = [0]
        copie = os.dup(w)

        def remplir() -> None:
            try:
                while True:
                    os.write(copie, b"x")
                    ecrits[0] += 1
            except OSError:
                pass
            finally:
                os.close(copie)

        threading.Thread(target=remplir, daemon=True, name="remplir-tube").start()
        dernier, stable_depuis = -1, time.monotonic()
        while time.monotonic() - stable_depuis < 0.3:
            time.sleep(0.05)
            if ecrits[0] != dernier:
                dernier, stable_depuis = ecrits[0], time.monotonic()
        assert ecrits[0] > 0, "tube non rempli"

    def test_arret_borne_stdout_et_stderr_bloques(self):
        """Tube de sortie plein et jamais lu (stdout et stderr) : le SIGTERM
        aboutit quand même, dans un délai borné, sans mort par signal."""
        r, w = os.pipe()
        proc = subprocess.Popen(
            [sys.executable, "-m", "ameesh.runner", "--agents", "personne", "--poll", "1"],
            stdout=w, stderr=w, env=self.env(), cwd=self.tmp)
        try:
            self._lire_jusqu_a(r, "LISTEN")
            self._remplir_tube(w)  # plus rien ne sort : toute écriture bloquerait
            debut = time.monotonic()
            proc.terminate()
            code = proc.wait(timeout=30)
            self.assertLess(time.monotonic() - debut, 15.0)
            self.assertEqual(code, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            os.close(r)
            os.close(w)

    def test_arret_borne_stderr_seul_bloque(self):
        r, w = os.pipe()
        proc = subprocess.Popen(
            [sys.executable, "-m", "ameesh.runner", "--agents", "personne", "--poll", "1"],
            stdout=subprocess.PIPE, stderr=w, text=True, env=self.env(), cwd=self.tmp)
        lignes: list[str] = []
        threading.Thread(
            target=lambda: [lignes.append(line) for line in proc.stdout], daemon=True
        ).start()
        try:
            self.wait_for(lambda: any("LISTEN" in l for l in lignes), timeout=20)
            self._remplir_tube(w)
            debut = time.monotonic()
            proc.terminate()
            code = proc.wait(timeout=30)
            self.assertLess(time.monotonic() - debut, 15.0)
            self.assertEqual(code, 0, "".join(lignes))
            time.sleep(0.2)
            self.assertTrue(any("shutdown: 0 lease" in l for l in lignes), "".join(lignes))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            proc.stdout.close()
            os.close(r)
            os.close(w)

    def test_exception_dans_run_journal_ferme_code_non_nul(self):
        """Une exception de `run` : le journal est quand même vidé et fermé, le
        code de sortie est non nul, sans SIGABRT (relecture codex2, B2)."""
        script = (
            "import sys\n"
            "from ameesh import runner\n"
            "def boom(self):\n"
            "    for i in range(300):\n"
            "        runner.log_async('m%d' % i)\n"
            "    raise RuntimeError('boom')\n"
            "runner.Runner.run = boom\n"
            "sys.exit(runner.main(sys.argv[1:]))\n")
        proc = subprocess.run(
            [sys.executable, "-c", script, "--agents", "personne"],
            capture_output=True, text=True, timeout=60, env=self.env(), cwd=self.tmp)
        sortie = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, sortie)
        self.assertIn("RuntimeError: boom", proc.stderr)
        self.assertIn("m299", proc.stdout)  # journal entièrement vidé avant la sortie
        self.assertNotIn("Fatal Python error", sortie)

    # -- NOTIFY ------------------------------------------------------------
    def test_notify_reveille_un_executeur_en_service(self):
        cwd = self._cwd("dort")
        self.register("dort", "claude", cwd=cwd)
        proc = self.runner_popen("--max-turns", "1", "--poll", "60")
        try:
            time.sleep(2.0)  # LISTEN établi (pilote psql : ~0,5 s), aucun tour à faire
            self.assertEqual(self.turns(), [])
            debut = time.monotonic()
            envoi = self.cli("send", "dort", "réveille-toi",
                             env=self.env(AGENT_MAIL_NAME="orchestrateur"))
            self.assertEqual(envoi.returncode, 0)
            sortie, _ = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        self.assertLess(time.monotonic() - debut, 25.0)
        self.assertEqual(proc.returncode, 0, sortie)
        self.assertIn("message for dort: waking the turn", sortie)
        tours = self.turns()
        self.assertEqual(len(tours), 1, sortie)
        # le contenu du message est dans la consigne (pas un renvoi vers inbox)
        self.assertIn("réveille-toi", tours[0]["argv"][-1])
        self.assertIn(adapters.AUTHORITY_NOTE, tours[0]["argv"][-1])
        # le message a été remis, le tour a été compté
        self.assertEqual(mail.unread(self.db, "dort"), [])
        self.assertEqual(int(registry.get(self.db, "dort")["turns"]), 1)

    def test_notify_lease_reveille_apres_liberation(self):
        """Un bail qui se libère réveille les autres exécuteurs de l'hôte.

        Le sondage est à 60 s : sans NOTIFY, le test ne passerait pas.
        """
        cwd = self._cwd("libre")
        self.register("libre", "claude", cwd=cwd, prompt="reprends")
        registry.upsert(self.db, "libre", harness="claude", host=self.cfg.host, cwd=cwd)
        registry.claim(self.db, "libre", "runner-parti", 300)

        proc = self.runner_popen("--max-turns", "1", "--poll", "60")
        try:
            time.sleep(2.0)  # LISTEN établi
            self.assertEqual(self.turns(), [], "rien ne doit tourner sous bail")
            debut = time.monotonic()
            registry.release(self.db, "libre", "runner-parti", 1)
            sortie, _ = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        self.assertLess(time.monotonic() - debut, 25.0)
        self.assertEqual(proc.returncode, 0, sortie)
        self.assertEqual(len(self.turns()), 1, sortie)

    # -- cycle de vie ------------------------------------------------------
    def test_agent_arrete_n_est_plus_reclame(self):
        self.register("fini", "claude", cwd=self._cwd("fini"), prompt="non")
        stop = self.runner("stop", "fini")
        self.assertEqual(stop.returncode, 0)
        self.assertEqual(registry.get(self.db, "fini")["status"], "stopped")
        proc = self.runner("--once")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self.turns(), [])

    def test_register_ecrit_modele_budget_et_consigne(self):
        self.register("riche", "codex", cwd=self._cwd("riche"), prompt="tour",
                      model="gpt-5", budget="3.5", chantier="nexlink")
        row = registry.get(self.db, "riche")
        self.assertEqual(row["harness"], "codex")
        self.assertEqual(row["model"], "gpt-5")
        self.assertEqual(float(row["budget_usd"]), 3.5)
        self.assertEqual(row["chantier"], "nexlink")
        self.assertEqual(row["pending_prompt"], "tour")
        self.assertEqual(row["status"], "queued")

    def test_anomalie_du_harnais_marque_bloque(self):
        cwd = self._cwd("plante")
        self.register("plante", "codex", cwd=cwd, prompt="tour")
        env = self.env(AMEESH_TEST_EXIT="2")
        proc = self.runner("--once", "--agents", "plante", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        row = registry.get(self.db, "plante")
        self.assertEqual(row["status"], "blocked")
        self.assertIn("code 2", row["last_error"])
        self.assertEqual(int(row["turns"]), 1)  # le tour a eu lieu, il a échoué
        # B6a : un tour en échec ne consomme pas la consigne
        self.assertEqual(row["pending_prompt"], "tour")
        self.assertIsNone(row["current_prompt"])

    def test_consigne_survit_a_un_harnais_tue(self):
        """B6a (variante SIGKILL) : le harnais tué ne fait pas perdre la consigne."""
        cwd = self._cwd("tue")
        self.register("tue", "claude", cwd=cwd, prompt="consigne d'un tour tué")
        env = self.env(AMEESH_TEST_KILL_SELF="1")
        proc = self.runner("--once", "--agents", "tue", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        row = registry.get(self.db, "tue")
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["pending_prompt"], "consigne d'un tour tué")
        self.assertIsNone(row["current_prompt"])

    def test_renouvellement_borne_par_l_echeance_meme_si_l_appel_bloque(self):
        """Sonde codex3 : un renew qui pend ne dépasse pas l'échéance du bail.

        #2600 : l'échéance est simulée (0,5 s de budget) ; seule la borne
        d'attente réelle de `_renew_call` reste à mesurer, avec une marge
        large, sans dépendre d'un bail réel.
        """
        horloge = HorlogeSimulee()
        runner = Runner(self.cfg, self.db, once=True)
        runner.clock = horloge
        worker = AgentWorker(
            runner,
            {"name": "golf", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() + 0.5},
        )

        def bloque(*_args, **_kwargs):
            time.sleep(30)
            return horloge.time() + 300

        fini = threading.Event()
        resultat: dict = {}

        def renouveler() -> None:
            resultat["ok"] = worker.renew()
            fini.set()

        with mock.patch.object(registry, "renew", side_effect=bloque):
            thread = threading.Thread(target=renouveler, daemon=True)
            thread.start()
            # Borne de sûreté : si l'appel n'était pas borné, il durerait les
            # 30 s du faux `renew` au lieu de rendre la main après le budget.
            self.assertTrue(fini.wait(3.0),
                            "l'appel a survécu à l'échéance du bail")
            thread.join(timeout=1.0)
        self.assertFalse(resultat.get("ok"))
        self.assertTrue(any(t.is_alive() for t in worker.renewals),
                        "l'appel de base doit rester détaché après le budget")
        self.assertTrue(worker.lease_lost.is_set())

    def test_le_harnais_recoit_son_bail_dans_l_environnement(self):
        """L'identité du harnais est liée au bail : nom, runner et epoch."""
        cwd = self._cwd("lie")
        self.register("lie", "claude", cwd=cwd, prompt="tour lié")
        proc = self.runner("--once", "--agents", "lie")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        env = self.turns()[0]["env"]
        self.assertEqual(env["AGENT_MAIL_NAME"], "lie")
        self.assertTrue(env["AMEESH_RUNNER_ID"])
        # le tour tourne sous le premier bail ; le rendre en sortant incrémente
        # l'epoch en base, donc on compare au bail du moment, pas à l'après.
        self.assertEqual(int(env["AMEESH_LEASE_EPOCH"]), 1)

    def test_identite_git_du_tour_vient_du_bail_pas_du_depot(self):
        """#834 : auteur ET committer d'une session, repris ou non.

        Deux worktrees du MÊME dépôt partagent `.git/config` : il y porte
        l'identité d'un autre agent (incident du 13/10/2026, où une session
        reprise sans identité commitait sous le nom d'un tiers). Le témoin
        fait committer chaque session elle-même puis relit `%an <%ae>` et
        `%cn <%ce>` : sans l'environnement du tour, c'est l'autre agent.
        """
        autre = "autre-agent"
        nom1, nom2 = "gitident1", "gitident2"
        commun = os.path.join(self.tmp, "commun")
        w1, w2 = self._cwd(nom1), self._cwd(nom2)
        base = dict(os.environ, GIT_AUTHOR_NAME="essai",
                    GIT_AUTHOR_EMAIL="essai@ameesh.invalid",
                    GIT_COMMITTER_NAME="essai",
                    GIT_COMMITTER_EMAIL="essai@ameesh.invalid")
        subprocess.run(["git", "init", "-q", commun], check=True)
        subprocess.run(["git", "-C", commun, "commit", "-q", "--allow-empty",
                        "-m", "base"], check=True, env=base)
        subprocess.run(["git", "-C", commun, "config", "user.name", autre], check=True)
        subprocess.run(["git", "-C", commun, "config", "user.email",
                        "%s@ameesh.invalid" % autre], check=True)
        for numero, (nom, dossier) in enumerate(((nom1, w1), (nom2, w2)), start=1):
            subprocess.run(["git", "-C", commun, "worktree", "add", "-q",
                            "-b", "b%d" % numero, dossier], check=True)
        trace = os.path.join(self.tmp, "attribution.txt")
        # la commande tourne dans le dossier de travail de la session
        travail = ("git commit -q --allow-empty -m tour && "
                   "git log -1 --format='%%an <%%ae>|%%cn <%%ce>' >> %s"
                   % shlex.quote(trace))
        env = self.env(AMEESH_TEST_RUN=travail)
        for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                    "GIT_COMMITTER_EMAIL"):
            env.pop(var, None)  # le lanceur des tests ne prête pas son identité
        self.register(nom1, "claude", cwd=w1, prompt="premier tour")
        proc = self.runner("--once", "--agents", nom1, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.register(nom1, "claude", cwd=w1, prompt="deuxième tour")
        proc = self.runner("--once", "--agents", nom1, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.register(nom2, "claude", cwd=w2, prompt="tour de l'autre agent")
        proc = self.runner("--once", "--agents", nom2, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        def attendu(nom: str) -> str:
            return "%s <%s@ameesh.invalid>|%s <%s@ameesh.invalid>" % (nom, nom, nom, nom)

        with open(trace, encoding="utf-8") as fh:
            lignes = fh.read().splitlines()
        # le premier agent deux fois (session neuve puis reprise), le second une
        self.assertEqual(lignes, [attendu(nom1), attendu(nom1), attendu(nom2)])
        # les quatre variables du contrat sont dans l'environnement du tour
        env_tour = [t for t in self.turns() if "env" in t][-1]["env"]
        self.assertEqual((env_tour["GIT_AUTHOR_NAME"], env_tour["GIT_AUTHOR_EMAIL"],
                          env_tour["GIT_COMMITTER_NAME"], env_tour["GIT_COMMITTER_EMAIL"]),
                         (nom2, "%s@ameesh.invalid" % nom2,
                          nom2, "%s@ameesh.invalid" % nom2))
        # la configuration partagée du dépôt n'a jamais été écrite
        garde = subprocess.run(["git", "-C", commun, "config", "user.name"],
                               capture_output=True, text=True, check=True)
        self.assertEqual(garde.stdout.strip(), autre)

    def test_config_partagee_signalee_jamais_utilisee(self):
        """#896 : un `user.name` laissé dans le `.git/config` partagé est signalé.

        Le tour ne s'en sert jamais (#834) ; le signalement couvre ce qui
        partirait hors harnais depuis ce dossier (incident du 13/10/2026).
        """
        autre, nom = "autre-agent", "gitident-garde"
        commun = os.path.join(self.tmp, "commun-garde")
        w = self._cwd(nom)
        base = dict(os.environ, GIT_AUTHOR_NAME="essai",
                    GIT_AUTHOR_EMAIL="essai@ameesh.invalid",
                    GIT_COMMITTER_NAME="essai",
                    GIT_COMMITTER_EMAIL="essai@ameesh.invalid")
        subprocess.run(["git", "init", "-q", commun], check=True)
        subprocess.run(["git", "-C", commun, "commit", "-q", "--allow-empty",
                        "-m", "base"], check=True, env=base)
        subprocess.run(["git", "-C", commun, "config", "user.name", autre], check=True)
        subprocess.run(["git", "-C", commun, "config", "user.email",
                        "%s@ameesh.invalid" % autre], check=True)
        subprocess.run(["git", "-C", commun, "worktree", "add", "-q", "-b", "garde", w],
                       check=True)
        env = self.env(AMEESH_TEST_RUN="true")
        for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                    "GIT_COMMITTER_EMAIL"):
            env.pop(var, None)  # le lanceur des tests ne prête pas son identité
        self.register(nom, "claude", cwd=w, prompt="tour avec configuration partagée")
        proc = self.runner("--once", "--agents", nom, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("shared git identity", proc.stdout)
        self.assertIn(autre, proc.stdout)
        # l'identité du tour reste celle du bail, pas celle du dépôt
        env_tour = [t for t in self.turns() if "env" in t][-1]["env"]
        self.assertEqual(env_tour["GIT_AUTHOR_NAME"], nom)

        # même dépôt, un agent qui porte le nom de la configuration : rien à signaler
        chez_lui = self._cwd(autre)
        subprocess.run(["git", "-C", commun, "worktree", "add", "-q",
                        "-b", "accord", chez_lui], check=True)
        self.register(autre, "claude", cwd=chez_lui, prompt="tour de l'agent nommé")
        proc = self.runner("--once", "--agents", autre, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("shared git identity", proc.stdout)

    def test_echeance_atteinte_arrete_sans_appeler_la_base(self):
        """Verdict codex3 B5a : plus de budget plancher après l'échéance.

        Réveil tardif : l'échéance est déjà passée, on n'appelle même pas
        `registry.renew` — on arrête le harnais immédiatement.
        """
        horloge = HorlogeSimulee()
        runner = Runner(self.cfg, self.db, once=True)
        runner.clock = horloge
        worker = AgentWorker(
            runner,
            {"name": "india", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() - 1},
        )
        with mock.patch.object(registry, "renew") as appel:
            self.assertFalse(worker.renew())
        appel.assert_not_called()
        self.assertTrue(worker.lease_lost.is_set())

    def test_budget_borne_par_le_temps_restant_sans_marge(self):
        """Le budget d'appel vaut le temps restant, pas un plancher de 0,5 s.

        Horloge simulée (#2600) : sous charge CI, 0,3 s de marge s'écoulaient
        entre la création du worker et les appels, et le budget valait 0.
        """
        horloge = HorlogeSimulee()
        runner = Runner(self.cfg, self.db, once=True)
        runner.clock = horloge
        worker = AgentWorker(
            runner,
            {"name": "juliette", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() + 0.3},
        )
        self.assertLessEqual(worker.call_budget(), 0.4)
        self.assertGreater(worker.call_budget(), 0.0)
        expire = AgentWorker(
            runner,
            {"name": "kilo", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() - 5},
        )
        self.assertTrue(expire.deadline_passed())
        self.assertEqual(expire.call_budget(), 0.0)  # aucun plancher au-delà

        # entré juste avant l'échéance : le budget vaut le temps restant exact
        juste = AgentWorker(
            runner,
            {"name": "lima", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() + 0.02},
        )
        self.assertLessEqual(juste.call_budget(), 0.03)

    def test_appel_entame_juste_avant_l_echeance_ne_survit_pas(self):
        """Verdict codex3 B5a : entré 20 ms avant l'échéance, l'appel est coupé.

        #2600 : l'échéance est simulée ; le budget d'appel (0,05 s) est réel
        et mesuré avec une marge large, sans bail réel de quelques secondes.
        """
        horloge = HorlogeSimulee()
        runner = Runner(self.cfg, self.db, once=True)
        runner.clock = horloge
        worker = AgentWorker(
            runner,
            {"name": "mike", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() + 0.05},
        )

        def bloque(*_args, **_kwargs):
            time.sleep(5)
            return horloge.time() + 300

        debut = time.monotonic()
        with mock.patch.object(registry, "renew", side_effect=bloque):
            self.assertFalse(worker.renew())
        ecoule = time.monotonic() - debut
        self.assertTrue(worker.lease_lost.is_set())
        self.assertLess(ecoule, 2.0, "l'appel ne doit pas survivre à l'échéance")

    def test_battement_se_reveille_a_l_echeance_du_bail(self):
        """B5a : with a long interval, the heartbeat still wakes at the deadline.

        #2600 : le battement est exécuté sur une horloge simulée, sans fil ni
        attente réelle ; l'échéance est atteinte au premier réveil.
        """
        horloge = HorlogeSimulee()
        runner = Runner(self.cfg, self.db, once=True)
        runner.lease_ttl = 300.0  # intervalle nominal de 100 s
        runner.clock = horloge
        worker = AgentWorker(
            runner,
            {"name": "delta", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() + 1},
        )
        stop = EvenementSimule(horloge)
        with mock.patch.object(AgentWorker, "ensure_watchdog"), \
                mock.patch.object(registry, "renew",
                                  side_effect=db_mod.DbError("base absente")):
            worker._heartbeat(stop)
        self.assertTrue(worker.lease_lost.is_set())
        self.assertLessEqual(horloge.attentes[0], 1.0,
                             "le battement a dormi au-delà de l'échéance")

    def test_battement_renouvelle_avant_l_echeance_d_un_bail_court(self):
        """#2483 : un bail de 3 s ne peut pas attendre un battement de 3 s.

        Le plancher de 3 s dormait jusqu'à l'échéance même ; sous charge CI le
        harnais n'était pas encore publié et le watchdog tuait le tour (run
        39133881211). #2600 : horloge simulée, un seul tour de boucle.
        """
        horloge = HorlogeSimulee()
        runner = Runner(self.cfg, self.db, once=True)
        runner.lease_ttl = 3.0  # comme --lease-ttl 3 des témoins de bail perdu
        runner.clock = horloge
        worker = AgentWorker(
            runner,
            {"name": "romeo", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() + 3},
        )
        stop = EvenementSimule(horloge)
        with mock.patch.object(AgentWorker, "ensure_watchdog"), \
                mock.patch.object(AgentWorker, "renew", return_value=False) as renew:
            worker._heartbeat(stop)
        renew.assert_called_once()
        attente = horloge.attentes[0]
        self.assertGreater(attente, 0.0)
        self.assertLessEqual(
            attente, 1.1,
            "un bail de 3 s doit être renouvelé avant son échéance, pas à "
            "l'échéance même : %ss" % attente)

    def test_attente_du_battement_ne_depasse_pas_l_echeance(self):
        """Verdict codex3 B5a : `wait_timeout` n'a aucun plancher positif.

        Nouvelle sonde réelle : bail à +20 ms ; un plancher de 50 ms laissait
        l'ancien harnais vivant et `lease_lost=False` après l'échéance, pendant
        qu'un vrai remplaçant réclamait l'agent. Ici le vrai `_heartbeat` reçoit
        l'attente calculée : elle vaut le temps restant (~20 ms), et exactement
        0 une fois l'échéance passée. #2600 : horloge simulée.
        """
        horloge = HorlogeSimulee()
        runner = Runner(self.cfg, self.db, once=True)
        runner.clock = horloge
        worker = AgentWorker(
            runner,
            {"name": "papa", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() + 0.02},
        )
        self.assertLessEqual(worker.wait_timeout(300), 0.03)
        self.assertGreaterEqual(worker.wait_timeout(300), 0.0)

        stop = EvenementSimule(horloge)
        with mock.patch.object(AgentWorker, "ensure_watchdog"), \
                mock.patch.object(AgentWorker, "renew", return_value=False) as renew:
            worker._heartbeat(stop)
        renew.assert_called_once()
        attente = horloge.attentes[0]
        self.assertLessEqual(attente, 0.03, "le battement dort au-delà de l'échéance")
        self.assertGreaterEqual(attente, 0.0)

        expire = AgentWorker(
            runner,
            {"name": "quebec", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() - 1},
        )
        self.assertEqual(expire.wait_timeout(300), 0.0)  # aucun plancher
        stop_expire = EvenementSimule(horloge)
        with mock.patch.object(AgentWorker, "ensure_watchdog"), \
                mock.patch.object(AgentWorker, "renew", return_value=False) as renew2:
            expire._heartbeat(stop_expire)
        self.assertEqual(horloge.attentes[-1], 0.0)
        renew2.assert_called_once()

    def test_branche_bail_perdu_ne_perd_pas_la_consigne(self):
        """Sonde codex3 (2) : la clôture d'un tour au bail perdu restaure la consigne.

        #2537 : en service, l'exécuteur reprend l'agent dès la passe suivante
        (reprise voulue, R5) et consomme la consigne restaurée ; un témoin qui
        échantillonne `pending_prompt` ne voit qu'un état transitoire et peut
        le rater sous charge CI. Le témoin fait donc UN seul tour (`--once`) :
        aucune reprise ne consomme la consigne, et l'état final se lit, stable,
        après la sortie de l'exécuteur.
        """
        cwd = self._cwd("perdu")
        self.register("perdu", "claude", cwd=cwd, prompt="consigne du tour perdu")
        # Le témoin mesure la clôture au bail perdu, pas la porte d'hôte réelle
        # de la machine (en CI, l'hôte se retire : draining, user_active).
        # Garde factice : un hôte prêté exige `file`, mais l'état lu est celui
        # du banc (disponible), pas le fichier réel de l'hôte.
        gate_state = os.path.join(self.tmp, "gate-available.json")
        with open(gate_state, "w", encoding="utf-8") as fh:
            fh.write('{"schema": "ameesh-host-state/1", "state": "available", "seq": 1}\n')
        # #2600 : bail de 20 s, pas 3 s — sous 8 passes parallèles, la
        # résolution du harnais et le pick dépassaient 3 s et le veilleur tuait
        # avant le tour. La cadence courte est couverte par les témoins à
        # horloge simulée ; ce témoin de bout en bout vérifie la restauration.
        env = self.env(AMEESH_TEST_SLEEP="300", AMEESH_LEASE_TTL="20",
                       AMEESH_HOST_GATE="file", AMEESH_HOST_GATE_PATH=gate_state,
                       AMEESH_HOST_GATE_ACK=os.path.join(self.tmp, "gate-ack", "ack.json"),
                       AMEESH_HOST_GATE_FALLBACK="available")
        proc = self.runner_popen("--once", "--agents", "perdu", "--lease-ttl", "20", env=env)
        # Le tuyau est lu en continu : un tampon plein ne bloque plus
        # l'exécuteur, et le témoin peut nommer sa sortie au refus.
        sortie_lignes: list[str] = []
        lecteur = threading.Thread(target=lambda: sortie_lignes.extend(proc.stdout or ()),
                                   daemon=True)
        lecteur.start()

        def arreter() -> None:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            lecteur.join(timeout=5)
            if proc.stdout is not None:
                proc.stdout.close()

        try:
            self.wait_for(lambda: len(self.turns()) == 1, timeout=60)
            self.db.execute(
                "UPDATE agent_registry SET lease_expires_at = now() - interval '1 second' "
                "WHERE name = 'perdu'")
            # Le harnais dort 300 s : un arrêt manqué à la perte du bail laisse
            # l'exécuteur vivant au-delà de cette borne. `--once` sort juste
            # après le tour : sa sortie est le repère stable (rien ne vient
            # reprendre la consigne restaurée). Sous charge CI, le battement
            # peut tarder : 120 s, et la sortie nomme la cause.
            self.wait_for(lambda: proc.poll() is not None, timeout=120)
            proc.wait(timeout=30)
        except AssertionError as exc:
            arreter()
            self.fail("%s\nsortie de l'exécuteur :\n%s"
                      % (exc, "".join(sortie_lignes[-60:])))
        finally:
            arreter()
        sortie = "".join(sortie_lignes)
        # pas de mort par signal à l'arrêt (SIGABRT du fil `journal`, 2026-10-05)
        self.assertGreaterEqual(proc.returncode, 0, "code %r\n%s" % (proc.returncode, sortie))
        self.assertNotIn("exiting immediately", sortie)
        # la perte de bail est nommée (toute issue admise par #2497)
        self.assertTrue(any(m in sortie for m in ARRETS_DE_BAIL),
                        "perte de bail non nommée :\n%s" % sortie)
        row = registry.get(self.db, "perdu")
        self.assertEqual(row["pending_prompt"], "consigne du tour perdu")
        self.assertIsNone(row["current_prompt"])
        self.assertEqual(int(row["turns"]), 0)

    def test_terminate_tue_le_groupe_de_processus(self):
        """Sonde codex3 (descendants) : un enfant du harnais ne survit pas."""
        pidfile = os.path.join(self.tmp, "enfant.pid")
        parent = subprocess.Popen(
            [sys.executable, "-c",
             "import os, signal, subprocess, sys, time;"
             "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
             "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
             "open(sys.argv[1], 'w').write(str(child.pid));"
             "time.sleep(60)", pidfile],
            start_new_session=True,
        )
        for _ in range(50):
            if os.path.exists(pidfile):
                break
            time.sleep(0.1)
        enfant_pid = int(open(pidfile, encoding="utf-8").read())
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "foxtrot", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 3600},
        )
        worker.proc = parent
        worker.pgid = parent.pid
        try:
            self.assertTrue(os_layer.alive(enfant_pid), "enfant mort avant l'arrêt")
            worker.terminate(grace=0)
            self.assertIsNotNone(parent.poll())
            self.wait_for(lambda: not os_layer.alive(enfant_pid), timeout=5)
        finally:
            if parent.poll() is None:
                parent.kill()
            try:
                os.kill(enfant_pid, signal.SIGKILL)
            except OSError:
                pass

    def test_terminate_tue_un_descendant_qui_ignore_sigterm(self):
        """Sonde codex3 B5b : le parent meurt sur SIGTERM, l'enfant l'ignore.

        L'escalade ne doit pas s'arrêter à la mort du parent.
        """
        pidfile = os.path.join(self.tmp, "enfant-recalcitrant.pid")
        parent = subprocess.Popen(
            [sys.executable, "-c",
             "import os, signal, subprocess, sys, time;"
             "child = subprocess.Popen([sys.executable, '-c',"
             " 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']);"
             "open(sys.argv[1], 'w').write(str(child.pid));"
             "time.sleep(60)", pidfile],
            start_new_session=True,
        )
        for _ in range(50):
            if os.path.exists(pidfile):
                break
            time.sleep(0.1)
        enfant_pid = int(open(pidfile, encoding="utf-8").read())
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "hotel", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 3600},
        )
        worker.proc = parent
        worker.pgid = parent.pid
        try:
            self.assertTrue(os_layer.alive(enfant_pid), "enfant mort avant l'arrêt")
            worker.terminate(grace=1.0)
            self.assertIsNotNone(parent.poll())
            self.wait_for(lambda: not os_layer.alive(enfant_pid), timeout=5)
        finally:
            if parent.poll() is None:
                parent.kill()
            try:
                os.kill(enfant_pid, signal.SIGKILL)
            except OSError:
                pass

    def test_terminate_escalade_vers_sigkill(self):
        """B5b : un harnais qui ignore SIGTERM est tué, pas laissé vivant."""
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
            start_new_session=True)  # comme un vrai harnais : son propre groupe
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "echo", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 3600},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        try:
            debut = time.monotonic()
            worker.terminate()
            # La mort du processus est un effet du SIGKILL, distinct du retour de
            # `terminate` : on attend la sortie (test stable sous charge), tout
            # en gardant la borne sémantique de l'escalade.
            self.wait_for(lambda: proc.poll() is not None, timeout=12, interval=0.02)
            self.assertLess(time.monotonic() - debut, 12.0)
            self.assertLess(proc.returncode, 0)  # tué par un signal (SIGKILL)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_terminate_moissonne_le_zombie_sans_attendre_deux_secondes(self):
        """Constat codex3 b731f4f : un zombie est compté vivant par killpg(0).

        `terminate(hard=True)` doit moissonner l'enfant direct après le SIGKILL ;
        sinon le zombie garde le groupe « vivant » et l'arrêt attend 2 s pour
        rien, avec un faux « groupe toujours vivant après SIGKILL ».
        """
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "romeo", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 3600},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        try:
            debut = time.monotonic()
            worker.terminate(grace=0, hard=True)
            ecoule = time.monotonic() - debut
            self.wait_for(lambda: proc.poll() is not None, timeout=3, interval=0.01)
            self.assertFalse(AgentWorker._group_alive(proc.pid))
            self.assertLess(ecoule, 1.0, "un zombie ne doit pas faire durer l'arrêt 2 s")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_le_sigkill_part_avant_tout_journal(self):
        """Sonde codex3 b731f4f : le journal avant le signal laissait, sous
        charge, un remplaçant réclamer pendant que l'ancien harnais vivait.

        Ici l'ordre est instrumenté : sur une échéance atteinte, le premier
        événement doit être le signal, pas une écriture de journal.

        #971 : `log_async` (le journal du chemin d'arrêt) est instrumenté à
        part, et les vidages tardifs du fil « journal » — messages mis en file
        par un test précédent — sont ignorés : l'ordre observé ne dépend plus
        de l'ordonnancement.
        """
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "tango", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() - 1},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        ordre: list[tuple] = []
        vrai_signal = AgentWorker._signal_group

        def signal_trace(pgid, cible, sig):
            ordre.append(("signal", sig))
            vrai_signal(pgid, cible, sig)

        def log_trace(message):
            # #971 : le fil « journal » peut vider ici un message mis en file
            # par un test précédent (le puits est un singleton de module) :
            # ce vidage tardif ne compte pas dans l'ordre de CE chemin d'arrêt.
            if threading.current_thread().name == "journal":
                return
            ordre.append(("log", message))

        def journal_trace(message):
            ordre.append(("journal", message))

        try:
            with mock.patch.object(AgentWorker, "_signal_group",
                                   staticmethod(signal_trace)), \
                    mock.patch("ameesh.runner.log", side_effect=log_trace), \
                    mock.patch("ameesh.runner.log_async", side_effect=journal_trace):
                self.assertFalse(worker.renew())
            self.assertTrue(worker.lease_lost.is_set())
            self.assertTrue(ordre, "l'arrêt doit être tracé")
            self.assertEqual(ordre[0][0], "signal", "un journal précède le SIGKILL")
            self.assertEqual(ordre[0][1], signal.SIGKILL)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_panne_de_base_definitive_tue_avant_tout_journal(self):
        """Revue codex3 de 852c8da : au 3e échec de base, le journal partait
        avant le test définitif et le SIGKILL.

        Un journal bloqué ne doit pas retarder l'arrêt : ici l'ordre est
        instrumenté, le premier événement doit être le signal.

        #971 : `log_async` (le journal du chemin d'arrêt) est instrumenté à
        part, et les vidages tardifs du fil « journal » — messages mis en file
        par un test précédent — sont ignorés : l'ordre observé ne dépend plus
        de l'ordonnancement.
        """
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        runner = Runner(self.cfg, self.db, once=True)
        worker = AgentWorker(
            runner,
            {"name": "uniform", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": time.time() + 3600},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        worker.renew_failures = AgentWorker.MAX_RENEW_FAILURES - 1
        ordre: list[tuple] = []
        vrai_signal = AgentWorker._signal_group

        def signal_trace(pgid, cible, sig):
            ordre.append(("signal", sig))
            vrai_signal(pgid, cible, sig)

        def log_trace(message):
            # #971 : le fil « journal » peut vider ici un message mis en file
            # par un test précédent (le puits est un singleton de module) :
            # ce vidage tardif ne compte pas dans l'ordre de CE chemin d'arrêt.
            if threading.current_thread().name == "journal":
                return
            ordre.append(("log", message))

        def journal_trace(message):
            ordre.append(("journal", message))

        try:
            with mock.patch.object(AgentWorker, "_signal_group",
                                   staticmethod(signal_trace)), \
                    mock.patch("ameesh.runner.log", side_effect=log_trace), \
                    mock.patch("ameesh.runner.log_async", side_effect=journal_trace), \
                    mock.patch.object(registry, "renew",
                                      side_effect=db_mod.DbError("base injoignable")):
                self.assertFalse(worker.renew())
            self.assertTrue(worker.lease_lost.is_set())
            self.assertTrue(ordre, "l'arrêt doit être tracé")
            self.assertEqual(ordre[0][0], "signal", "un journal précède le SIGKILL")
            self.assertEqual(ordre[0][1], signal.SIGKILL)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_journal_bloque_ne_bloque_pas_le_battement(self):
        """Sonde codex3 B5a-N : un puits de journal saturé ne doit pas
        immobiliser le fil du battement.

        Le diagnostic d'une erreur non définitive passe par une file bornée
        (perte comptée) : `renew` rend la main tout de suite, puis l'échéance
        tue quand même le harnais. #2600 : l'échéance est simulée, la borne
        réelle (10 s) laisse une marge large au puits bloqué (30 s).
        """
        horloge = HorlogeSimulee()
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        runner = Runner(self.cfg, self.db, once=True)
        runner.clock = horloge
        worker = AgentWorker(
            runner,
            {"name": "xray", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() + 30},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        bloque = threading.Event()

        def puits_bloque(message):
            bloque.wait(30)  # le puits ne rend pas la main

        try:
            with mock.patch("ameesh.runner.log", side_effect=puits_bloque), \
                    mock.patch.object(registry, "renew",
                                      side_effect=db_mod.DbError("base injoignable")):
                fini = threading.Event()
                resultat: dict = {}

                def renouveler() -> None:
                    resultat["ok"] = worker.renew()
                    fini.set()

                thread = threading.Thread(target=renouveler, daemon=True)
                thread.start()
                # Borne de sûreté : un `renew` qui attendrait le puits
                # bloquerait ici les 30 s de `puits_bloque`.
                self.assertTrue(fini.wait(10.0),
                                "le journal bloqué immobilise le battement")
                thread.join(timeout=1.0)
                self.assertTrue(resultat.get("ok"))  # 1er échec : non définitif
                self.assertFalse(worker.lease_lost.is_set())
                # l'échéance tombe : le battement doit encore pouvoir arrêter
                worker.lease_deadline = horloge.time() - 1
                bloque.set()  # libère le puits pour la fin du test
                self.assertFalse(worker.renew())
            self.assertTrue(worker.lease_lost.is_set())
            self.wait_for(lambda: proc.poll() is not None, timeout=5, interval=0.02)
        finally:
            bloque.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_veilleur_arrete_meme_battement_bloque(self):
        """Sonde codex3 B5a-N traitée par classe : le veilleur d'échéance est
        indépendant du battement.

        Même si `renew` ne rend jamais la main (journal, requête, fichier),
        le harnais est arrêté à l'échéance connue du bail. #2600 : le veilleur
        est exécuté sur une horloge simulée, le battement reste réellement
        bloqué dans `renew` ; aucune attente réelle de bail.
        """
        horloge = HorlogeSimulee()
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        runner = Runner(self.cfg, self.db, once=True)
        runner.clock = horloge
        worker = AgentWorker(
            runner,
            {"name": "whiskey", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() + 0.3},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        worker.watchdog_stop = EvenementSimule(horloge)
        bloque = threading.Event()
        dans_renew = threading.Event()
        stop = threading.Event()

        def renew_bloque():
            dans_renew.set()
            return bloque.wait(10)

        try:
            with mock.patch.object(AgentWorker, "ensure_watchdog"), \
                    mock.patch.object(AgentWorker, "renew", side_effect=renew_bloque):
                thread = threading.Thread(target=worker._heartbeat, args=(stop,), daemon=True)
                thread.start()
                self.assertTrue(dans_renew.wait(10.0),
                                "le battement n'est pas entré dans renew")
                worker._deadline_watchdog()
            self.assertTrue(worker.lease_lost.is_set())
            self.wait_for(lambda: proc.poll() is not None, timeout=5, interval=0.02)
            self.assertIsNotNone(proc.poll(), "le veilleur doit tuer à l'échéance")
        finally:
            stop.set()
            bloque.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_veilleur_ne_tue_pas_un_bail_renouvele(self):
        """Le veilleur n'est pas un minuteur : une échéance repoussée par un
        renouvellement réussi ne doit pas tuer le harnais.

        #2600 : l'horloge avance dans le `wait` du veilleur ; un callback simule
        le renouvellement (échéance repoussée) bien au-delà de l'ancienne.
        """
        horloge = HorlogeSimulee()
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        runner = Runner(self.cfg, self.db, once=True)
        runner.clock = horloge
        worker = AgentWorker(
            runner,
            {"name": "yankee", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() + 0.6},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        ancienne = worker.lease_deadline

        def renouvellement(h):
            if h.maintenant >= ancienne:
                worker.lease_deadline = h.maintenant + 1.5  # renouvellement réussi
            if len(h.attentes) >= 5:
                worker.watchdog_stop.set()

        worker.watchdog_stop = EvenementSimule(horloge, apres=renouvellement)
        try:
            worker._deadline_watchdog()
            self.assertFalse(worker.lease_lost.is_set(), "un bail renouvelé a été tué")
            self.assertIsNone(proc.poll(), "le harnais doit rester vivant")
        finally:
            worker.watchdog_stop.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_veilleur_reste_actif_pendant_l_arret_escalade(self):
        """Sonde codex3 B5a-S : `stopping` (arrêt gracieux) ne doit pas
        relâcher le veilleur.

        Un harnais qui ignore SIGTERM vit pendant les 3 s de grâce ; si le bail
        expire dans cette fenêtre, un remplaçant peut le réclamer. Le veilleur
        doit rester actif jusqu'à la mort du harnais ou le relâchement du bail.
        #2600 : l'échéance du veilleur est simulée ; l'escalade réelle reste
        mesurée sur la mort du processus.
        """
        horloge = HorlogeSimulee()
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
             " time.sleep(60)"],
            start_new_session=True)
        runner = Runner(self.cfg, self.db, once=True)
        runner.clock = horloge
        worker = AgentWorker(
            runner,
            {"name": "zulu", "harness": "claude"},
            {"lease_epoch": 1, "lease_expires_ts": horloge.time() + 0.3},
        )
        worker.proc = proc
        worker.pgid = proc.pid
        worker.watchdog_stop = EvenementSimule(horloge)
        try:
            worker.stopping.set()
            arret = threading.Thread(target=worker.terminate,
                                     kwargs={"grace": 60}, daemon=True)
            arret.start()
            worker._deadline_watchdog()
            self.assertTrue(worker.lease_lost.is_set())
            self.wait_for(lambda: proc.poll() is not None, timeout=5, interval=0.02)
            arret.join(timeout=5)
        finally:
            worker.watchdog_stop.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    #: échéance de publication du harnais : borne de sûreté du fil sonde,
    #: jamais une mesure du produit (un tour peut être lent sous charge).
    PUBLICATION_DELAI = 10.0

    #: borne de la mise à mort après publication : bien en deçà du sommeil du
    #: faux harnais (`AMEESH_TEST_SLEEP` = 5 s), donc un harnais encore vivant
    #: à l'échéance prouve que le tour l'a attendu au lieu de le tuer.
    MORT_HARNAIS_DELAI = 2.0

    def _popen_du_harnais(self, publie: dict, apres_lancement):
        """Remplace `subprocess.Popen` pendant un tour : le harnais seul.

        `ameesh.runner.subprocess` est le module stdlib : patcher `Popen`
        intercepte **tous** les sous-processus du processus, les requêtes du
        pilote `psql` comprises — une attente dans le wrapper faisait alors
        dépendre la durée du tour du nombre d'allers-retours en base (flocon
        du publication_tardive sous psql). Seul le lancement du harnais (le
        seul à demander sa propre session) est intercepté : `apres_lancement`
        court après le vrai `Popen` et avant la publication de `self.proc`,
        donc l'ordre « le déclencheur conclut avant la publication » est
        garanti sans aucune attente dormante.
        """
        vrai_popen = subprocess.Popen

        def popen(*args, **kwargs):
            proc = vrai_popen(*args, **kwargs)
            if kwargs.get("start_new_session"):  # le harnais (pas le pilote psql)
                publie["proc"] = proc
                apres_lancement(proc)
            return proc

        return mock.patch("ameesh.runner.subprocess.Popen", side_effect=popen)

    def _surveille_mort(self, publie: dict) -> threading.Event:
        """Sonde la mort du harnais publié, sans attente dormante fixe.

        Le fil rend la main dès que `publie["proc"]` est publié et mort, dans
        `MORT_HARNAIS_DELAI` ; l'échéance court à partir de la publication,
        jamais du début du tour, et `run_turn` peut finir sa comptabilité
        (sous psql, une requête par échange) sans peser sur la mesure.
        """
        mort = threading.Event()

        def sonde():
            debut = time.monotonic()
            while "proc" not in publie and time.monotonic() - debut < self.PUBLICATION_DELAI:
                time.sleep(0.005)
            proc = publie.get("proc")
            if proc is None:
                return
            fin = time.monotonic() + self.MORT_HARNAIS_DELAI
            while time.monotonic() < fin:
                if proc.poll() is not None:
                    mort.set()
                    return
                time.sleep(0.005)

        threading.Thread(target=sonde, daemon=True,
                         name="sonde-mort-harnais").start()
        return mort

    def test_harnais_publie_apres_la_perte_du_bail_est_tue(self):
        """Sonde codex3 B5a-P : course de publication du Popen.

        Le veilleur conclut pendant que `Popen` n'est pas encore revenu (il voit
        `proc=None`) ; si la publication de `self.proc` ne recontrôle pas, un
        harnais sans stdout survit au bail jusqu'à la fin du tour.
        """
        cwd = self._cwd("pubrace")
        self.register("pubrace", "claude", cwd=cwd, prompt="tour")
        runner = Runner(self.cfg, self.db, once=True)
        lease = registry.claim(self.db, "pubrace", runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        agent = registry.get(self.db, "pubrace")
        worker = AgentWorker(runner, agent, lease)
        publie: dict = {}
        mort = self._surveille_mort(publie)

        def veilleur_a_conclu(_proc):
            worker.lease_lost.set()  # le veilleur a conclu avant la publication

        fakebin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakebin")
        env = {"AMEESH_BIN_DIR": fakebin, "AMEESH_TEST_SLEEP": "5",
               "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                    self._popen_du_harnais(publie, veilleur_a_conclu):
                ok = worker.run_turn({"kind": "prompt", "prompt": "tour", "ids": []})
            self.assertFalse(ok)
            self.assertIn("proc", publie, "le harnais doit avoir été lancé")
            self.assertTrue(mort.wait(self.MORT_HARNAIS_DELAI),
                            "le harnais publié doit être tué, pas attendu")
            self.assertEqual(publie["proc"].returncode, -signal.SIGKILL)
            self.assertEqual(registry.get(self.db, "pubrace").get("pending_prompt"), "tour")
        finally:
            if worker.proc is not None and worker.proc.poll() is None:
                worker.proc.kill()
                worker.proc.wait(timeout=5)
            worker.watchdog_stop.set()

    def test_arret_relache_le_bail_apres_avoir_tue_le_harnais(self):
        """Le relâchement du bail ne doit jamais laisser un harnais vivant.

        Le terminate initial peut tomber avant la publication du `Popen` ; un
        arrêt dur après la jointure garantit que le bail n'est rendu qu'une fois
        le harnais tué (course de publication, sonde codex3 B5a-P).
        """
        runner = Runner(self.cfg, self.db, once=True)
        ordre: list[tuple] = []

        class FauxWorker:
            def __init__(self):
                self.stopping = threading.Event()
                self.wake = threading.Event()

            def terminate(self, grace=None, hard=False):
                ordre.append(("terminate", hard))

            def stop_group_now(self, reason, grace=None, hard=False):
                # point de passage unique : le double enregistre l'ordre réel
                ordre.append(("stop", reason, hard))
                self.terminate(grace=grace, hard=hard)

            def join(self, timeout=None):
                pass

            def release_lease(self):
                ordre.append(("release", None))

        runner.workers = {"fantome": FauxWorker()}
        runner.shutdown()
        self.assertIn(("terminate", True), ordre)
        self.assertLess(ordre.index(("terminate", True)), ordre.index(("release", None)))

    def test_publication_tardive_apres_relachement_est_tuee(self):
        """Sonde codex3 B5a-Q : une publication tardive après un shutdown qui a
        rendu le bail et arrêté le veilleur doit être tuée, même si l'ancienne
        échéance n'est pas encore atteinte.
        """
        cwd = self._cwd("pubshutdown")
        self.register("pubshutdown", "claude", cwd=cwd, prompt="tour")
        runner = Runner(self.cfg, self.db, once=True)
        lease = registry.claim(self.db, "pubshutdown", runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        agent = registry.get(self.db, "pubshutdown")
        worker = AgentWorker(runner, agent, lease)
        publie: dict = {}
        mort = self._surveille_mort(publie)

        def shutdown_a_conclu(_proc):
            # shutdown a rendu le bail et arrêté le veilleur pendant la création
            worker.stopping.set()
            worker.watchdog_stop.set()

        fakebin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakebin")
        env = {"AMEESH_BIN_DIR": fakebin, "AMEESH_TEST_SLEEP": "5",
               "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                    self._popen_du_harnais(publie, shutdown_a_conclu):
                ok = worker.run_turn({"kind": "prompt", "prompt": "tour", "ids": []})
            self.assertFalse(ok)
            self.assertIn("proc", publie, "le harnais doit avoir été lancé")
            self.assertTrue(mort.wait(self.MORT_HARNAIS_DELAI),
                            "le harnais publié doit être tué, pas attendu")
            self.assertEqual(publie["proc"].returncode, -signal.SIGKILL)
            self.assertTrue(worker.lease_lost.is_set())
        finally:
            if worker.proc is not None and worker.proc.poll() is None:
                worker.proc.kill()
                worker.proc.wait(timeout=5)
            worker.watchdog_stop.set()



class JournalArretTest(unittest.TestCase):
    """Le fil `journal` se vide puis s'arrête avant la finalisation (SIGABRT)."""

    def test_close_vide_la_file_puis_arrete_le_fil(self):
        recus: list[str] = []
        with mock.patch("ameesh.runner.log", side_effect=recus.append):
            journal = _AsyncLog()
            for i in range(50):
                journal.write("m%d" % i)
            self.assertTrue(journal.close(timeout=5))
            self.assertFalse(journal.thread.is_alive())
            self.assertEqual(recus, ["m%d" % i for i in range(50)])
            # après fermeture, un fil démon tardif n'écrit plus rien
            journal.write("après")
            self.assertTrue(journal.close(timeout=5))
        self.assertNotIn("après", recus)
        self.assertEqual(journal.perdus, 1)

    def test_close_borne_si_le_puits_bloque(self):
        libere = threading.Event()
        with mock.patch("ameesh.runner.log", side_effect=lambda _m: libere.wait(10)):
            journal = _AsyncLog(maxsize=1)
            journal.write("bloque")
            time.sleep(0.1)
            journal.write("en file")
            debut = time.monotonic()
            self.assertFalse(journal.close(timeout=0.3))
            self.assertLess(time.monotonic() - debut, 2.0)
            libere.set()
            self.assertTrue(journal.close(timeout=5))  # puits libéré : arrêt normal

if __name__ == "__main__":
    unittest.main()
