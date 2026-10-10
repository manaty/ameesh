# SPDX-License-Identifier: AGPL-3.0-only
"""Le courrier d'un tour est DANS la consigne (correctif du pilote de bascule).

Pendant la coexistence v0/v1, `agent-mail inbox` lit la boîte fichier v0 : une
consigne qui y renvoie ne montrait jamais les messages v1, pourtant marqués
livrés. Désormais : contenu dans la consigne, plafond de taille, livraison au
lancement du harnais seulement, réservation fencée par le bail, et le hook ne
re-livre pas ce qui est dans la consigne.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock

from ameesh import adapters, mail, registry, runner as runner_mod
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase

#: faux harnais : dépose un message « pendant le tour », appelle le hook v1
#: comme le ferait le harnais, journalise sa sortie, puis se comporte comme le
#: faux claude du banc (flux JSONL, journal de l'argv)
HARNAIS_AVEC_HOOK = r'''#!%(python)s
import json, os, subprocess, sys
nom = os.environ["AGENT_MAIL_NAME"]
tiers = dict(os.environ)
for cle in ("AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID", "AMEESH_LEASE_EPOCH",
            "AGENT_MESH_LEASE_EPOCH"):
    tiers.pop(cle, None)
tiers["AGENT_MAIL_NAME"] = "tiers"
subprocess.run([sys.executable, "-m", "ameesh.cli", "send", nom,
                "arrivé pendant le tour"], env=tiers, check=True,
               capture_output=True)
hook = subprocess.run([sys.executable, "-m", "ameesh.cli", "hook", "claude"],
                      input=json.dumps({"hook_event_name": "UserPromptSubmit",
                                        "cwd": os.getcwd()}),
                      capture_output=True, text=True)
with open(os.environ["HOOK_LOG"], "a", encoding="utf-8") as fh:
    fh.write(hook.stdout)
os.execv(%(faux)r, [%(faux)r] + sys.argv[1:])
'''


class CourrierDansLaConsigneTest(PgTestCase):
    def _cwd(self, name: str) -> str:
        path = os.path.join(self.tmp, "work", name)
        os.makedirs(path, exist_ok=True)
        return path

    def _worker(self, name: str, *, senders=(), **reglages):
        cfg = dataclasses.replace(self.cfg, interrupt_senders=tuple(senders))
        runner = Runner(cfg, self.db, once=True)
        runner.event_coalesce = 0.0
        for cle, valeur in reglages.items():
            setattr(runner, cle, valeur)
        self.register(name, "claude", cwd=self._cwd(name))
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return runner, worker

    def _env_harnais(self, **extra):
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=False)

    def _ligne(self, message_id: int) -> dict:
        return mail.get(self.db, message_id)

    def _consigne(self) -> str:
        tours = self.turns()
        self.assertTrue(tours, "aucun tour lancé")
        return tours[-1]["argv"][-1]

    # -- contenu ------------------------------------------------------------
    def test_contenu_du_message_dans_la_consigne(self):
        self.register("lecteur", "claude", cwd=self._cwd("lecteur"))
        mid = mail.send(self.db, "orch", "lecteur", "ligne une\nligne deux du message v1")
        proc = self.runner("--once", "--agents", "lecteur")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        consigne = self._consigne()
        self.assertIn("> ligne une\n> ligne deux du message v1", consigne)
        self.assertIn("message n°%d de orch" % mid, consigne)
        ligne = self._ligne(mid)
        # horodatage lisible (ISO local, à la seconde)
        import datetime
        attendu = datetime.datetime.fromtimestamp(
            ligne["created_ts"]).astimezone().isoformat(timespec="seconds")
        self.assertIn(attendu, consigne)
        self.assertIn(adapters.AUTHORITY_NOTE, consigne)
        self.assertIn("jamais l'autorité du propriétaire", consigne)
        self.assertNotIn("lance agent-mail inbox", consigne)
        self.assertIsNotNone(ligne["delivered_ts"])
        self.assertEqual(mail.unread(self.db, "lecteur"), [])

    def test_corps_illisible_non_reproduit(self):
        _runner, worker = self._worker("illisible")
        mid = mail.send(self.db, "src", "illisible", '{"secret": "zzz-payload"}',
                        kind="event", payload={"cache": "yyy-payload"},
                        allow_structured=True)
        spec = worker.pick()
        self.assertEqual(spec["ids"], [mid])
        self.assertIn("corps non lisible", spec["prompt"])
        self.assertNotIn("zzz-payload", spec["prompt"])
        self.assertNotIn("yyy-payload", spec["prompt"])

    # -- plafond --------------------------------------------------------------
    def test_plafond_sur_le_rendu_reel_en_octets(self):
        """Corps à lignes courtes (le préfixe « > » pèse) et accentués (2 octets) :
        chaque consigne ENTIÈRE tient dans le plafond, les plus anciens d'abord,
        aucun message perdu ni doublé, le reste annoncé."""
        self.register("plafond", "claude", cwd=self._cwd("plafond"))
        corps = ["message numéro %02d\n" % i + "\n".join(["é"] * 120) for i in range(12)]
        ids = [mail.send(self.db, "orch", "plafond", c) for c in corps]
        plafond = 2000
        env = self.env(AMEESH_PROMPT_MAIL_MAX=str(plafond))
        vus: list[int] = []
        tours = 0
        while mail.unread(self.db, "plafond") and tours < 12:
            avant = [m["id"] for m in mail.unread(self.db, "plafond")]
            proc = self.runner("--once", "--agents", "plafond", env=env)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            tours += 1
            consigne = self._consigne()
            self.assertLessEqual(mail.octets(consigne), plafond)
            inclus = [i for i in ids if "message n°%d de" % i in consigne]
            self.assertTrue(inclus)
            self.assertEqual(inclus, avant[:len(inclus)])        # plus anciens d'abord
            reste = len(avant) - len(inclus)
            if reste:
                self.assertIn(adapters.MAIL_REMAINING % reste, consigne)
            else:
                self.assertNotIn("autre(s) message(s)", consigne)
            vus += inclus
        self.assertGreater(tours, 1)
        self.assertEqual(vus, ids)                                # ni perte ni doublon
        self.assertEqual(mail.unread(self.db, "plafond"), [])

    def test_plafond_compte_le_resume_de_reprise(self):
        resume = "état du lot : " + "décision prise. " * 30
        # le plafond laisse au courrier un peu plus que son minimum, cadre de
        # reprise et résumé compris (le cadre est compté, quelle que soit sa taille)
        plafond = mail.octets(adapters.resume_prompt(resume)) + mail.PROMPT_MIN_BYTES + 500
        _runner, worker = self._worker("resume", prompt_max_bytes=plafond)
        worker.resume_summary = resume
        for i in range(10):
            mail.send(self.db, "orch", "resume", "note %d " % i + "à relire " * 30)
        spec = worker.pick()
        with self._env_harnais():
            self.assertTrue(worker.run_turn(spec))
        consigne = self._consigne()
        self.assertTrue(consigne.startswith("Reprise de session après rotation"))
        self.assertLessEqual(mail.octets(consigne), plafond)
        self.assertIn("note 0 ", consigne)
        # le courrier suit le bloc du résumé, jamais dedans
        self.assertLess(consigne.index("</resume-de-session>"), consigne.index("note 0 "))
        self.assertLess(len(spec["ids"]), 10, "le plafond a bien retenu du courrier")

    def test_message_trop_long_tronque_mais_jamais_bloquant(self):
        _runner, worker = self._worker("long", prompt_max_bytes=2000)
        mid = mail.send(self.db, "orch", "long", "phrase longue àéè\n" * 400)
        spec = worker.pick()
        self.assertEqual(spec["ids"], [mid])
        self.assertIn("tronqué", spec["prompt"])
        self.assertIn("message n°%d" % mid, spec["prompt"])
        self.assertLessEqual(mail.octets(spec["prompt"]), 2000)

    def test_prompt_for_unitaire(self):
        rows = [{"id": i, "sender": "s", "created_ts": 0.0, "body": "x" * 300}
                for i in range(20)]
        texte, retenus, reste = mail.prompt_for(None, rows, kind="mail", budget=2500)
        self.assertLessEqual(mail.octets(texte), 2500)
        self.assertEqual(len(retenus) + reste, 20)
        self.assertEqual([r["id"] for r in retenus], list(range(len(retenus))))
        texte, retenus, reste = mail.prompt_for(
            None, [{"id": 9, "body": "ÿ" * 50000}], kind="urgent", budget=2000)
        self.assertEqual(([r["id"] for r in retenus], reste), ([9], 0))
        self.assertLessEqual(mail.octets(texte), 2000)

    # -- livraison au lancement ------------------------------------------------
    def test_livre_seulement_au_lancement_du_harnais(self):
        _runner, worker = self._worker("lancement")
        mid = mail.send(self.db, "orch", "lancement", "à livrer au lancement")
        spec = worker.pick()
        etat_au_lancement: list = []
        vrai_popen = subprocess.Popen

        def popen(*args, **kwargs):
            if kwargs.get("start_new_session"):  # le harnais (pas le pilote psql)
                etat_au_lancement.append(self._ligne(mid))
            return vrai_popen(*args, **kwargs)

        with self._env_harnais(), mock.patch.object(runner_mod.subprocess, "Popen",
                                                    side_effect=popen):
            self.assertTrue(worker.run_turn(spec))
        avant = etat_au_lancement[0]
        # au lancement : réservé pour ce bail, pas encore livré
        self.assertIsNone(avant["delivered_ts"])
        self.assertTrue(avant["consigne_jeton"])
        self.assertEqual(avant["consigne_runner"], worker.runner.runner_id)
        self.assertEqual(int(avant["consigne_epoch"]), worker.epoch)
        self.assertIsNotNone(self._ligne(mid)["delivered_ts"])

    def test_harnais_absent_le_courrier_reste_non_livre(self):
        _runner, worker = self._worker("absent")
        mid = mail.send(self.db, "orch", "absent", "jamais vu")
        spec = worker.pick()
        with self._env_harnais(AMEESH_CLAUDE_BIN=os.path.join(self.tmp, "inexistant")):
            self.assertFalse(worker.run_turn(spec))
        ligne = self._ligne(mid)
        self.assertIsNone(ligne["delivered_ts"])
        self.assertIsNone(ligne["consigne_runner"])   # réservation libérée
        self.assertFalse(ligne["deja_consigne"])      # rien n'a été vu
        self.assertEqual(self.turns(), [])
        # le tour suivant le livre, sans mention « re-livré » — L106 : une fois
        # le harnais retrouvé, à l'échéance du nouvel essai (hôte non prêt)
        with self._env_harnais():
            worker._host_next = 0.0
            spec = worker.pick()
        self.assertNotIn("re-livré", spec["prompt"])
        with self._env_harnais():
            self.assertTrue(worker.run_turn(spec))
        self.assertIn("jamais vu", self._consigne())
        self.assertIsNotNone(self._ligne(mid)["delivered_ts"])

    def test_echec_du_lancement_libere_la_reservation(self):
        _runner, worker = self._worker("oserror")
        mid = mail.send(self.db, "orch", "oserror", "lancement raté")
        spec = worker.pick()
        vrai_popen = subprocess.Popen

        def popen(*args, **kwargs):
            if kwargs.get("start_new_session"):  # le harnais (pas le pilote psql)
                raise OSError("exec raté")
            return vrai_popen(*args, **kwargs)

        with self._env_harnais(), mock.patch.object(
                runner_mod.subprocess, "Popen", side_effect=popen):
            # L106 : un lancement impossible est une erreur de l'hôte, plus une
            # exception qui tue le worker
            self.assertFalse(worker.run_turn(spec))
        self.assertFalse(worker.fast_failure)
        self.assertEqual(registry.get(self.db, "oserror")["status_text"], "hôte non prêt")
        ligne = self._ligne(mid)
        self.assertIsNone(ligne["delivered_ts"])
        self.assertIsNone(ligne["consigne_runner"])

    def test_dossier_absent_le_courrier_reste_non_livre(self):
        _runner, worker = self._worker("sansdossier")
        mid = mail.send(self.db, "orch", "sansdossier", "à garder")
        spec = worker.pick()
        worker.agent["cwd"] = os.path.join(self.tmp, "nulle-part")
        with self._env_harnais(), mock.patch.object(worker, "adopt_moved_worktree",
                                                    return_value=None):
            self.assertFalse(worker.run_turn(spec))
        self.assertIsNone(self._ligne(mid)["delivered_ts"])
        self.assertIsNone(self._ligne(mid)["consigne_runner"])

    def test_tour_en_echec_apres_lancement_reste_livre(self):
        _runner, worker = self._worker("echoue")
        mid = mail.send(self.db, "orch", "echoue", "vu puis échec")
        spec = worker.pick()
        with self._env_harnais(AMEESH_TEST_EXIT="3"):
            self.assertFalse(worker.run_turn(spec))
        # le harnais a reçu la consigne : livré, pas re-servi
        self.assertIsNotNone(self._ligne(mid)["delivered_ts"])
        self.assertIsNone(worker.pick())

    # -- bail, epoch, reprise après panne ---------------------------------------
    def _reserve(self, name, owner, epoch, ids=None, *, db=None, porteur="consigne",
                 jeton=None):
        jeton = jeton or mail.new_token()
        rows = mail.reserve(db or self.db, name, owner, epoch, jeton, ids=ids,
                            porteur=porteur)
        return jeton, [int(r["id"]) for r in rows]

    def test_reservation_fencee_par_le_bail(self):
        _runner, worker = self._worker("fence")
        mid = mail.send(self.db, "orch", "fence", "fencé")
        rid = worker.runner.runner_id
        self.assertEqual(self._reserve("fence", "autre-runner", worker.epoch, [mid])[1], [])
        self.assertEqual(self._reserve("fence", rid, worker.epoch + 1, [mid])[1], [])
        jeton, ids = self._reserve("fence", rid, worker.epoch, [mid])
        self.assertEqual(ids, [mid])
        # déjà réservé (réservation active) : jamais réservé deux fois
        self.assertEqual(self._reserve("fence", rid, worker.epoch, [mid])[1], [])
        self.assertEqual(self._reserve("fence", None, None, [mid], porteur="hook")[1], [])
        # seule la remise qui porte le jeton solde
        self.assertEqual(mail.deliver(self.db, "fence", rid, worker.epoch, "autre", [mid]), [])
        self.assertEqual(mail.deliver(self.db, "fence", rid, worker.epoch, jeton, [mid]), [mid])

    def test_reservation_attend_le_verrou_du_registre_et_voit_le_bail_courant(self):
        """B1 : la réservation verrouille la ligne du registre puis contrôle le
        bail sur sa version courante — une reprise de bail concurrente, encore
        non validée, la fait attendre puis échouer (deux connexions réelles)."""
        _runner, worker = self._worker("verrou")
        mid = mail.send(self.db, "orch", "verrou", "course au bail")
        autre = self.connect()
        self.addCleanup(autre.close)
        resultat: dict = {}

        def reserve():
            conn = self.connect()
            try:
                resultat["ids"] = self._reserve("verrou", worker.runner.runner_id,
                                                worker.epoch, [mid], db=conn)[1]
            except BaseException as exc:
                resultat["ids"] = exc
            finally:
                conn.close()

        with autre.transaction() as tx:
            tx.query("SELECT name FROM agent_registry WHERE name = 'verrou' FOR UPDATE")
            tx.execute("UPDATE agent_registry SET lease_owner = 'repreneur', "
                       "lease_epoch = lease_epoch + 1 WHERE name = 'verrou'")
            fil_r = threading.Thread(target=reserve)
            fil_r.start()
            fil_r.join(1.5)
            self.assertTrue(fil_r.is_alive(), "la réservation n'a pas attendu le verrou")
        fil_r.join(30)
        self.assertEqual(resultat["ids"], [])
        self.assertIsNone(self._ligne(mid)["consigne_runner"])

    def test_remise_d_un_ancien_bail_refusee(self):
        """B2 : un ancien exécuteur ne solde pas une réservation après la perte
        de son bail ; le message reste non livré et sera re-livré, signalé."""
        _runner, worker = self._worker("ancien")
        mid = mail.send(self.db, "orch", "ancien", "remise incertaine")
        rid = worker.runner.runner_id
        jeton, ids = self._reserve("ancien", rid, worker.epoch, [mid])
        self.assertEqual(ids, [mid])
        self.db.execute("UPDATE agent_registry SET lease_owner = 'nouveau', "
                        "lease_epoch = lease_epoch + 1, lease_expires_at = now() + "
                        "interval '1 hour' WHERE name = 'ancien'")
        self.assertEqual(mail.deliver(self.db, "ancien", rid, worker.epoch, jeton, [mid]), [])
        self.assertIsNone(self._ligne(mid)["delivered_ts"])
        # le nouveau bail la reprend (réservation d'un autre epoch : inactive)
        epoch2 = int(registry.get(self.db, "ancien")["lease_epoch"])
        jeton2 = mail.new_token()
        rows = mail.reserve(self.db, "ancien", "nouveau", epoch2, jeton2, porteur="consigne")
        self.assertEqual([int(r["id"]) for r in rows], [mid])
        self.assertTrue(rows[0]["deja_consigne"])
        # et l'ancien jeton ne solde toujours rien
        self.assertEqual(mail.deliver(self.db, "ancien", rid, worker.epoch, jeton, [mid]), [])
        self.assertEqual(mail.deliver(self.db, "ancien", "nouveau", epoch2, jeton2, [mid]),
                         [mid])

    def test_bail_perdu_au_lancement_remise_signalee_incertaine(self):
        _runner, worker = self._worker("perdu")
        mid = mail.send(self.db, "orch", "perdu", "vu sans solde")
        spec = worker.pick()
        vrai_popen = subprocess.Popen

        def popen(*args, **kwargs):
            proc = vrai_popen(*args, **kwargs)
            if kwargs.get("start_new_session"):   # le harnais : le bail change de main
                self.db.execute("UPDATE agent_registry SET lease_owner = 'autre', "
                                "lease_epoch = lease_epoch + 1 WHERE name = 'perdu'")
            return proc

        with self._env_harnais(), mock.patch.object(runner_mod.subprocess, "Popen",
                                                    side_effect=popen), \
                mock.patch.object(runner_mod, "log") as journal:
            worker.run_turn(spec)
        self.assertIsNone(self._ligne(mid)["delivered_ts"])
        self.assertTrue(any("remise incertaine" in str(c) for c in journal.call_args_list))

    def test_panne_apres_reservation_re_livre_en_le_signalant(self):
        _runner, worker = self._worker("panne")
        mid = mail.send(self.db, "orch", "panne", "peut-être déjà vu")
        # l'exécuteur réserve puis tombe (ni remise ni annulation)
        self.assertEqual(self._reserve("panne", worker.runner.runner_id, worker.epoch,
                                       [mid])[1], [mid])
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - "
                        "interval '1 second' WHERE name = 'panne'")
        runner2 = Runner(self.cfg, self.db, once=True)
        lease2 = registry.claim(self.db, "panne", runner2.runner_id, 3600)
        self.assertIsNotNone(lease2)
        worker2 = AgentWorker(runner2, registry.get(self.db, "panne"), lease2)
        self.addCleanup(worker2.watchdog_stop.set)
        self.assertGreater(worker2.epoch, worker.epoch)
        spec = worker2.pick()
        self.assertEqual(spec["ids"], [mid])            # aucune perte
        self.assertIn("re-livré", spec["prompt"])        # doublon signalé
        with self._env_harnais():
            self.assertTrue(worker2.run_turn(spec))
        self.assertIsNotNone(self._ligne(mid)["delivered_ts"])
        consigne = self._consigne()
        self.assertIn("peut-être déjà vu", consigne)
        self.assertIn("re-livré", consigne)

    def test_reservation_expiree_reprise_et_signalee(self):
        _runner, worker = self._worker("expire")
        mid = mail.send(self.db, "orch", "expire", "réservation expirée")
        jeton = mail.new_token()
        mail.reserve(self.db, "expire", worker.runner.runner_id, worker.epoch, jeton,
                     porteur="hook", ttl_seconds=-1)
        rows = mail.reserve(self.db, "expire", worker.runner.runner_id, worker.epoch,
                            mail.new_token(), porteur="consigne")
        self.assertEqual([int(r["id"]) for r in rows], [mid])
        self.assertTrue(rows[0]["deja_consigne"])

    def test_hook_d_un_autre_bail_livre_une_reservation_orpheline(self):
        _runner, worker = self._worker("orphelin")
        mid = mail.send(self.db, "orch", "orphelin", "réservation orpheline")
        self._reserve("orphelin", worker.runner.runner_id, worker.epoch, [mid])
        self.db.execute("UPDATE agent_registry SET lease_epoch = lease_epoch + 1 "
                        "WHERE name = 'orphelin'")
        sortie = self._hook("orphelin", worker.runner.runner_id, worker.epoch + 1)
        self.assertIn("réservation orpheline", sortie)
        self.assertIn("re-livré", sortie)
        self.assertIsNotNone(self._ligne(mid)["delivered_ts"])

    # -- hook ---------------------------------------------------------------
    def _hook(self, name: str, runner_id: str, epoch: int,
              event: str = "UserPromptSubmit") -> str:
        env = self.env(AGENT_MAIL_NAME=name, AMEESH_RUNNER_ID=runner_id,
                       AMEESH_LEASE_EPOCH=str(epoch))
        proc = self.cli("hook", "claude", env=env,
                        stdin=json.dumps({"hook_event_name": event}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_hook_ne_livre_pas_ce_qui_est_reserve_pour_la_consigne(self):
        _runner, worker = self._worker("hookres")
        dans_la_consigne = mail.send(self.db, "orch", "hookres", "déjà dans la consigne")
        self._reserve("hookres", worker.runner.runner_id, worker.epoch, [dans_la_consigne])
        nouveau = mail.send(self.db, "orch", "hookres", "nouveau pendant le tour")
        sortie = self._hook("hookres", worker.runner.runner_id, worker.epoch)
        self.assertIn("nouveau pendant le tour", sortie)
        self.assertNotIn("déjà dans la consigne", sortie)
        self.assertIsNotNone(self._ligne(nouveau)["delivered_ts"])
        # le hook n'a pas touché au message réservé : c'est le lancement qui le livre
        self.assertIsNone(self._ligne(dans_la_consigne)["delivered_ts"])
        # le Stop hook non plus
        self.assertEqual(self._hook("hookres", worker.runner.runner_id, worker.epoch,
                                    "Stop"), "")

    def test_hook_et_executeur_en_concurrence_une_seule_remise(self):
        """B3 : exécuteur et hook réservent en même temps, sur deux connexions
        réelles ; chaque message est réservé par UN seul porteur, et seule la
        remise qui porte le jeton le solde."""
        _runner, worker = self._worker("course")
        rid, epoch = worker.runner.runner_id, worker.epoch
        ids = [mail.send(self.db, "orch", "course", "message %d" % i) for i in range(30)]
        barriere = threading.Barrier(2)
        resultats: dict = {}

        def porteur(nom, porte):
            conn = self.connect()
            try:
                barriere.wait(10)
                jeton = mail.new_token()
                pris: list[int] = []
                for _ in range(10):
                    rows = mail.reserve(conn, "course", rid, epoch, jeton, porteur=porte,
                                        limit=3)
                    pris += [int(r["id"]) for r in rows]
                resultats[nom] = (jeton, pris,
                                  mail.deliver(conn, "course", rid, epoch, jeton, pris))
            except BaseException as exc:
                resultats[nom] = exc
            finally:
                conn.close()

        fils = [threading.Thread(target=porteur, args=("executeur", "consigne")),
                threading.Thread(target=porteur, args=("hook", "hook"))]
        for f in fils:
            f.start()
        for f in fils:
            f.join(60)
        for valeur in resultats.values():
            if isinstance(valeur, BaseException):
                raise valeur
        (j1, p1, l1), (j2, p2, l2) = resultats["executeur"], resultats["hook"]
        self.assertFalse(set(p1) & set(p2), "message réservé deux fois")
        self.assertEqual(sorted(p1 + p2), ids)
        self.assertEqual((sorted(l1), sorted(l2)), (sorted(p1), sorted(p2)))
        # un jeton ne solde jamais la réservation de l'autre
        self.assertEqual(mail.deliver(self.db, "course", rid, epoch, j1, p2), [])
        self.assertEqual(mail.unread(self.db, "course"), [])

    def test_hook_pendant_le_tour_sans_doublon_de_bout_en_bout(self):
        script = os.path.join(self.tmp, "claude-avec-hook")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(HARNAIS_AVEC_HOOK % {"python": sys.executable,
                                          "faux": os.path.join(FAKEBIN, "claude")})
        os.chmod(script, 0o755)
        hook_log = os.path.join(self.tmp, "hook.log")
        self.register("bout", "claude", cwd=self._cwd("bout"))
        avant = mail.send(self.db, "orch", "bout", "message de la consigne")
        env = self.env(AMEESH_CLAUDE_BIN=script, HOOK_LOG=hook_log)
        proc = self.runner("--once", "--agents", "bout", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("message de la consigne", self._consigne())
        with open(hook_log, encoding="utf-8") as fh:
            sortie_hook = fh.read()
        self.assertIn("arrivé pendant le tour", sortie_hook)
        self.assertNotIn("message de la consigne", sortie_hook)
        self.assertIsNotNone(self._ligne(avant)["delivered_ts"])
        self.assertEqual(mail.unread(self.db, "bout"), [])
        self.assertEqual(len(self.turns()), 1)

    # -- événements et prioritaire -------------------------------------------
    def test_evenements_regroupes_dans_la_consigne(self):
        self.register("evts", "claude", cwd=self._cwd("evts"))
        e1 = mail.send(self.db, "ci", "evts", "build 41 vert", kind="event")
        e2 = mail.send(self.db, "ci", "evts", "build 42 rouge", kind="event")
        proc = self.runner("--once", "--agents", "evts",
                           env=self.env(AMEESH_EVENT_COALESCE="0"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        consigne = self._consigne()
        self.assertIn(adapters.EVENT_HEADER % 2, consigne)
        self.assertIn("build 41 vert", consigne)
        self.assertIn("build 42 rouge", consigne)
        self.assertIn("(événement)", consigne)
        self.assertIn(adapters.AUTHORITY_NOTE, consigne)
        for mid in (e1, e2):
            self.assertIsNotNone(self._ligne(mid)["delivered_ts"])
        self.assertEqual(len(self.turns()), 1)

    def test_prioritaire_dans_la_consigne_et_livre(self):
        _runner, worker = self._worker("prio", senders=("orch",))
        mid = mail.send(self.db, "orch", "prio", "arrête tout et relance la CI",
                        kind="event", payload={"urgent": True})
        spec = worker.pick()
        self.assertEqual(spec["kind"], "urgent")
        with self._env_harnais():
            self.assertTrue(worker.run_turn(spec))
        consigne = self._consigne()
        self.assertIn(adapters.PRIORITY_HEADER % 1, consigne)
        self.assertIn("> arrête tout et relance la CI", consigne)
        self.assertIn("de orch", consigne)
        self.assertIn(adapters.AUTHORITY_NOTE, consigne)
        self.assertIn("urgent", consigne)
        self.assertIsNotNone(self._ligne(mid)["delivered_ts"])

    def test_dry_run_ne_livre_rien(self):
        self.register("sec", "claude", cwd=self._cwd("sec"))
        mid = mail.send(self.db, "orch", "sec", "pas en dry-run")
        proc = self.runner("--once", "--dry-run", "--agents", "sec")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIsNone(self._ligne(mid)["delivered_ts"])
        self.assertIsNone(self._ligne(mid)["consigne_runner"])


if __name__ == "__main__":
    unittest.main()
