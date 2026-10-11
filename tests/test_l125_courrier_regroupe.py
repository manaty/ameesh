# SPDX-License-Identifier: AGPL-3.0-only
"""L125 — regrouper le courrier avant de réveiller un agent.

Constat (un projet de 13 agents, quatre heures) : 1 270 messages, 504 tours,
60 % des tours de moins de 2 min, 87 % des tours de courrier ouverts par un
seul message ; les copies à l'orchestrateur, les accusés de réception et les
diffusions à tous ouvraient chacun des tours. Désormais : une fenêtre de
regroupement avant de réveiller un agent au repos (un message urgent ou d'un
humain réveille tout de suite), des accusés, copies et diffusions passifs —
lus au tour suivant —, et l'alerte `turn_churn` pour mesurer l'effet.
Postgres réel ; le faux harnais `claude` du banc pour le seul tour lancé.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import time
import unittest
from unittest import mock

from ameesh import adapters, cli, exploitation, mail, registry, storage
from ameesh import config as config_mod
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase


def _ligne(kind: str = "notify", payload: dict | None = None, body: str = "texte") -> dict:
    return {"id": 1, "sender": "pair", "kind": kind, "payload": payload or {},
            "body": body, "created_ts": 0.0}


class AccuseDeReceptionTest(unittest.TestCase):
    """La règle prudente de `mail.looks_like_ack` (sans base)."""

    def test_accuses_reconnus(self):
        for texte in ("Reçu, merci.", "merci !", "Merci beaucoup 🙏", "Bien reçu, merci",
                      "C'est noté, merci.", "noté", "Merci pour ton retour !",
                      "Bien reçu ton message, merci", "Thanks!", "Received, thank you.",
                      "ack", "Accusé de réception.", "Je te remercie",
                      "Merci pour l'info"):
            self.assertTrue(mail.looks_like_ack(texte), texte)

    def test_ce_qui_n_en_est_jamais(self):
        for texte in (
                "ok", "OK merci", "d'accord", "oui", "go", "Parfait, merci",  # feux verts
                "Pas reçu", "Rien reçu, merci de renvoyer", "Non merci",       # négations
                "Merci, peux-tu relancer les tests ?", "reçu ?",               # questions
                "Reçu, je m'en occupe", "Merci, c'est fait",                   # informations
                "Reçu le lot 12, merci", "Merci deepseek3",                    # références
                "👍", "", "   ",                                                # rien à lire
                "Merci " + "beaucoup " * 20,                                   # trop long
                "merci 谢谢"):                                                  # autre écriture
            self.assertFalse(mail.looks_like_ack(texte), texte)

    def test_passif_et_nature(self):
        self.assertEqual(mail.passive_reason(_ligne(payload={"ack": True})), "ack")
        self.assertEqual(mail.passive_reason(_ligne("event", {"cc": "dev"})), "cc")
        self.assertEqual(mail.passive_reason(_ligne(payload={"broadcast": True})), "broadcast")
        # un urgent n'est jamais passif ; un message ordinaire non plus
        self.assertEqual(mail.passive_reason(
            _ligne(payload={"broadcast": True, "urgent": True})), "")
        self.assertEqual(mail.passive_reason(_ligne()), "")
        self.assertEqual(mail.passive_reason(_ligne(payload='{"ack": true}')), "ack")
        self.assertEqual(mail.passive_label(_ligne("event", {"cc": "dev"})),
                         "copie d'un message à dev")
        self.assertEqual(mail.passive_label(_ligne("event", {"cc": "x y"})),
                         "copie d'un message à ?")
        rendu = mail.prompt_for(None, [_ligne("event", {"cc": "dev"}),
                                       _ligne(payload={"ack": True}),
                                       _ligne(payload={"broadcast": True}),
                                       _ligne(payload={"urgent": True}),
                                       _ligne("event")], kind="mail")[0]
        for nature in ("(copie d'un message à dev)", "(accusé de réception)",
                       "(diffusion à tous)", "(urgent)", "(événement)"):
            self.assertIn(nature, rendu)
        self.assertTrue(mail.is_human(_ligne(payload={"human": True})))
        self.assertFalse(mail.is_human(_ligne()))

    def test_la_consigne_du_tour_rappelle_la_regle(self):
        for option in ("--ack", "--cc", "--urgent"):
            self.assertIn(option, adapters.MAIL_FOOTER)


class ReglageParDefautTest(unittest.TestCase):
    def test_defaut_et_environnement(self):
        base = {"AMEESH_CONFIG": "/nulle-part.json"}
        self.assertEqual(config_mod.load(env=base).mail_batch, 90.0)
        self.assertEqual(config_mod.load(env=dict(base, AMEESH_MAIL_BATCH="45")).mail_batch,
                         45.0)
        self.assertEqual(config_mod.load(env=dict(base, AMEESH_MAIL_BATCH="0")).mail_batch,
                         0.0)
        for mauvais in ("nan", "-3", "inf", "bientôt"):
            self.assertEqual(
                config_mod.load(env=dict(base, AMEESH_MAIL_BATCH=mauvais)).mail_batch,
                90.0, mauvais)


class _Base(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM turn_resources")

    def _worker(self, name: str, *, batch: float = 60.0, humans: str = "",
                senders=()):
        cfg = dataclasses.replace(self.cfg, humans=humans,
                                  interrupt_senders=tuple(senders))
        runner = Runner(cfg, self.db, once=True)
        runner.mail_batch = batch
        runner.event_coalesce = 120.0
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return runner, worker

    def _vieillir(self, name: str, secondes: float) -> None:
        """Le courrier non lu de `name` est arrivé il y a `secondes`."""
        self.db.execute(
            "UPDATE agent_mailbox SET created_at = now() - make_interval(secs => %s)"
            " WHERE recipient = %s AND delivered_at IS NULL", (float(secondes), name))


class FenetreDeRegroupementTest(_Base):
    def test_un_seul_tour_pour_le_courrier_de_la_fenetre(self):
        _runner, worker = self._worker("orch")
        premier = mail.send(self.db, "dev1", "orch", "le lot 12 est prêt")
        self.assertIsNone(worker.pick())  # fenêtre en cours : pas de réveil
        second = mail.send(self.db, "dev2", "orch", "le lot 13 est prêt")
        self.assertIsNone(worker.pick())
        self._vieillir("orch", 61)
        spec = worker.pick()
        self.assertEqual(spec["kind"], "mail")
        self.assertEqual(sorted(spec["ids"]), sorted([premier, second]))
        self.assertIn("le lot 12", spec["prompt"])
        self.assertIn("le lot 13", spec["prompt"])

    def test_urgent_et_humain_reveillent_tout_de_suite(self):
        _runner, worker = self._worker("orch-u")
        mail.send(self.db, "dev1", "orch-u", "en attente")
        self.assertIsNone(worker.pick())
        # urgent d'un expéditeur NON habilité : réveil sans délai, sans
        # interrompre (pas de tour prioritaire)
        mail.send(self.db, "dev2", "orch-u", "le build de main est cassé",
                  payload={"urgent": True})
        spec = worker.pick()
        self.assertEqual(spec["kind"], "mail")
        self.assertEqual(len(spec["ids"]), 2)

        _runner, worker = self._worker("orch-h", humans="proprio")
        mail.send(self.db, "proprio", "orch-h", "fais le point")      # déclaré (AMEESH_HUMANS)
        self.assertEqual(worker.pick()["kind"], "mail")
        _runner, worker = self._worker("orch-h2")
        mail.send(self.db, "alice", "orch-h2", "fais le point", payload={"human": True})
        self.assertEqual(worker.pick()["kind"], "mail")              # marqué à l'envoi

    def test_fenetre_nulle_et_reglage_local(self):
        runner, worker = self._worker("orch-0")
        mail.send(self.db, "dev1", "orch-0", "tout de suite ?")
        self.assertIsNone(worker.pick())
        # réglage de l'agent (état local, `ameesh set … mail_batch=0`)
        with open(worker._path("mail_batch"), "w", encoding="utf-8") as fh:
            fh.write("0\n")
        self.assertEqual(worker.mail_batch(), 0.0)
        self.assertEqual(worker.pick()["kind"], "mail")
        # illisible : ignoré, défaut de l'exécuteur
        mail.send(self.db, "dev1", "orch-0", "encore")
        with open(worker._path("mail_batch"), "w", encoding="utf-8") as fh:
            fh.write("bientôt\n")
        self.assertEqual(worker.mail_batch(), 60.0)
        self.assertIsNone(worker.pick())
        runner.mail_batch = 0.0  # défaut de l'hôte : réveil immédiat (avant L125)
        self.assertEqual(worker.pick()["kind"], "mail")

    def test_horloge_de_la_base_en_avance(self):
        # La base date le message d'une heure dans le futur : l'attente se
        # compte aussi depuis que l'exécuteur l'a vu, jamais plus d'une fenêtre.
        _runner, worker = self._worker("orch-t", batch=0.3)
        mail.send(self.db, "dev1", "orch-t", "décalé")
        self._vieillir("orch-t", -3600)
        self.assertIsNone(worker.pick())
        time.sleep(0.4)
        self.assertEqual(worker.pick()["kind"], "mail")

    def test_un_evenement_du_emporte_le_courrier_en_attente(self):
        runner, worker = self._worker("orch-e")
        mail.send(self.db, "dev1", "orch-e", "en attente")
        mail.send(self.db, "ameesh", "orch-e", "capacité au repos", kind="event")
        spec = worker.pick()  # aucun réveil d'événements récent : l'événement est dû
        self.assertEqual(spec["kind"], "mail")
        self.assertEqual(len(spec["ids"]), 2)
        self.assertGreater(registry.get(self.db, "orch-e")["last_event_ts"] or 0, 0)

    def test_tour_de_bout_en_bout(self):
        """L'exécuteur (`--once`) attend la fenêtre puis lance UN tour."""
        name = "orch-run"
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        mail.send(self.db, "dev1", name, "premier message")
        mail.send(self.db, "dev2", name, "second message")
        debut = time.monotonic()
        proc = self.runner("--once", "--wait", "8", "--agents", name,
                           env=self.env(AMEESH_MAIL_BATCH="1.5"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertGreaterEqual(time.monotonic() - debut, 1.0)
        [tour] = self.turns()
        self.assertIn("premier message", tour["argv"][-1])
        self.assertIn("second message", tour["argv"][-1])
        self.assertEqual(mail.unread(self.db, name), [])


class CourrierPassifTest(_Base):
    def _passifs(self, name: str) -> list[int]:
        return [
            mail.send(self.db, "dev1", name, "Bien reçu, merci", payload={"ack": True}),
            mail.send(self.db, "dev1", name, "dev2 : prends le lot 14", kind="event",
                      payload={"cc": "dev2"}),
            mail.send(self.db, "dev3", name, "Revue à 15 h", payload={"broadcast": True}),
        ]

    def test_le_passif_seul_n_ouvre_pas_de_tour(self):
        runner, worker = self._worker("orch-p", batch=0.0)
        runner.event_coalesce = 0.0
        ids = self._passifs("orch-p")
        self._vieillir("orch-p", 3600)
        self.assertIsNone(worker.pick())
        self.assertFalse(worker.peek())
        # … il part avec le tour suivant, dans l'ordre, avec sa nature
        ids.append(mail.send(self.db, "dev2", "orch-p", "le lot 14 est fini"))
        self.assertTrue(worker.peek())
        spec = worker.pick()
        self.assertEqual(spec["kind"], "mail")
        self.assertEqual(sorted(spec["ids"]), sorted(ids))
        for nature in ("(accusé de réception)", "(copie d'un message à dev2)",
                       "(diffusion à tous)"):
            self.assertIn(nature, spec["prompt"])

    def test_le_message_du_passe_avant_le_passif_quand_la_consigne_est_pleine(self):
        runner, worker = self._worker("orch-b", batch=0.0)
        runner.prompt_max_bytes = 3000
        for _ in range(4):
            mail.send(self.db, "dev1", "orch-b", "copie volumineuse " + "x " * 600,
                      kind="event", payload={"cc": "dev2"})
        du = mail.send(self.db, "dev2", "orch-b", "question courte pour toi")
        spec = worker.pick()
        self.assertIn(du, spec["ids"])
        self.assertGreater(spec["remaining"], 0)

    def test_une_boite_pleine_de_passif_ne_cache_pas_le_message_du(self):
        _runner, worker = self._worker("orch-f", batch=0.0)
        for index in range(mail.UNREAD_LIMIT):
            mail.send(self.db, "dev1", "orch-f", "copie n°%d" % index, kind="event",
                      payload={"cc": "dev2"}, thread=False)
        self.assertIsNone(worker.pick())
        self.assertFalse(worker.peek())
        du = mail.send(self.db, "dev2", "orch-f", "question pour toi")
        self.assertTrue(worker.peek())
        spec = worker.pick()
        self.assertIn(du, spec["ids"])
        self.assertIn("question pour toi", spec["prompt"])

    def test_la_relance_d_inactivite_reste_possible(self):
        runner, worker = self._worker("orch-i", batch=0.0)
        self._passifs("orch-i")
        runner.idle_nudge = 0.0
        self.assertEqual(worker.pick()["kind"], "idle")

    def test_listing_et_alertes_ignorent_le_passif(self):
        registry.upsert(self.db, "orch-a", harness="claude")
        self._passifs("orch-a")
        self._vieillir("orch-a", 3600)
        [row] = [r for r in storage.of(self.db).operations.listing() if r["name"] == "orch-a"]
        self.assertEqual((row["unread"], row["passive_unread"]), (0, 3))
        self.assertIsNone(row["oldest_unread_ts"])
        alertes = exploitation.alerts(self.cfg, self.db)
        self.assertFalse([a for a in alertes if a["type"] == "idle_with_mail"
                          and a["agent"] == "orch-a"])
        mail.send(self.db, "dev1", "orch-a", "message ordinaire")
        self._vieillir("orch-a", 3600)
        alertes = exploitation.alerts(self.cfg, self.db)
        [alerte] = [a for a in alertes if a["type"] == "idle_with_mail"
                    and a["agent"] == "orch-a"]
        self.assertEqual(alerte["value"], 1)


class EnvoiTest(_Base):
    def _envoi(self, *args, sender: str = "dev1", **env):
        return self.cli("send", *args, env=self.env(AGENT_MAIL_NAME=sender, **env))

    def _payload(self, recipient: str) -> dict:
        return mail.message_payload(mail.unread(self.db, recipient)[-1])

    def test_accuse_explicite_ou_reconnu(self):
        for name in ("orch", "dev1"):
            registry.upsert(self.db, name, harness="claude")
        proc = self._envoi("orch", "Bien reçu, merci !")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("accusé de réception", proc.stdout)
        self.assertTrue(self._payload("orch").get("ack"))
        proc = self._envoi("orch", "ok")  # une réponse, peut-être un feu vert
        self.assertEqual(proc.stdout.strip(), "déposé pour : orch")
        self.assertEqual(self._payload("orch"), {})
        proc = self._envoi("orch", "J'ai lu ta note sur le lot 12", "--ack")
        self.assertTrue(self._payload("orch").get("ack"))
        self.assertEqual(self._envoi("orch", "x", "--ack", "--urgent").returncode, 2)
        # un humain réveille toujours : jamais d'accusé reconnu pour lui
        proc = self.cli("send", "orch", "Merci !", "--from", "proprio",
                        env=self.env(AMEESH_HUMANS="proprio"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._payload("orch"), {"human": True})

    def test_copie_sans_reveil(self):
        for name in ("orch", "dev1", "dev2"):
            registry.upsert(self.db, name, harness="claude")
        proc = self._envoi("dev2", "prends le lot 14", "--cc", "orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("copie sans réveil pour : orch", proc.stdout)
        principal = mail.unread(self.db, "dev2")[-1]
        self.assertEqual(mail.passive_reason(principal), "")
        [copie] = mail.unread(self.db, "orch")
        self.assertTrue(mail.is_event(copie))
        self.assertEqual(mail.passive_reason(copie), "cc")
        self.assertEqual(mail.message_payload(copie)["cc"], "dev2")
        self.assertIsNone(self.db.query("SELECT work_item_id FROM agent_mailbox WHERE id = %s",
                                        (int(copie["id"]),))[0]["work_item_id"])
        # refus avant tout dépôt
        avant = len(mail.unread(self.db, "dev2"))
        proc = self._envoi("dev2", "x", "--cc", "inconnu")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("agent inconnu", proc.stderr)
        self.assertEqual(len(mail.unread(self.db, "dev2")), avant)
        proc = self._envoi("dev2,orch", "x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--cc orch", proc.stderr)
        self.assertEqual(self._envoi("all", "x", "--cc", "orch").returncode, 2)

    def test_diffusion_lue_au_tour_suivant(self):
        for name in ("orch", "dev1", "dev2"):
            registry.upsert(self.db, name, harness="claude")
        proc = self._envoi("all", "Revue à 15 h", sender="orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("annonce", proc.stdout)
        for name in ("dev1", "dev2"):
            self.assertEqual(mail.passive_reason(mail.unread(self.db, name)[-1]), "broadcast")
        proc = self._envoi("all", "Arrêtez de pousser sur main", "--urgent", sender="orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in ("dev1", "dev2"):
            payload = self._payload(name)
            self.assertEqual((payload.get("urgent"), payload.get("broadcast")), (True, None))


class HookTest(_Base):
    def _hook(self, worker, event: str) -> str:
        env = self.env(AGENT_MAIL_NAME=worker.name, AMEESH_RUNNER_ID=worker.runner.runner_id,
                       AMEESH_LEASE_EPOCH=str(worker.epoch))
        proc = self.cli("hook", "claude", env=env, stdin=json.dumps({"hook_event_name": event}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_le_passif_ne_relance_pas_un_tour_qui_finit(self):
        _runner, worker = self._worker("orch-k")
        copie = mail.send(self.db, "dev1", "orch-k", "dev2 : prends le lot 14",
                          kind="event", payload={"cc": "dev2"})
        self.assertEqual(self._hook(worker, "Stop"), "")
        self.assertEqual([int(m["id"]) for m in mail.unread(self.db, "orch-k")], [copie])
        # rien ne reste réservé : la consigne du tour suivant le prendra
        jeton = mail.new_token()
        self.assertEqual(len(mail.reserve(self.db, "orch-k", worker.runner.runner_id,
                                          worker.epoch, jeton, porteur="consigne")), 1)
        mail.release(self.db, "orch-k", jeton, [copie])
        # pendant le tour, le hook le remet, avec sa nature
        sortie = json.loads(self._hook(worker, "PostToolUse"))
        texte = sortie["hookSpecificOutput"]["additionalContext"]
        self.assertIn("(copie d'un message à dev2) : dev2 : prends le lot 14", texte)
        # un message ordinaire relance le Stop, comme avant
        mail.send(self.db, "dev2", "orch-k", "le lot 14 est fini")
        self.assertEqual(json.loads(self._hook(worker, "Stop"))["decision"], "block")

    def test_rendu_v0_inchange_pour_un_message_ordinaire(self):
        texte = cli.render([{"from": "dev1", "ts": 0, "text": "bonjour"}])
        self.assertIn("— de dev1 à %s : bonjour" % time.strftime(
            "%H:%M", time.localtime(0)), texte)


class TurnChurnTest(_Base):
    def _tours(self, agent: str, durees, debut_s: float = 1800.0) -> None:
        tr = storage.of(self.db).turn_resources
        for index, duree in enumerate(durees):
            turn_id = "%s-%d" % (agent, index)
            tr.open_turn(turn_id, agent, "h1", pgid=None, label=None)
            self.db.execute(
                "UPDATE turn_resources SET status = 'done',"
                " started_at = now() - make_interval(secs => %s),"
                " ended_at = now() - make_interval(secs => %s) WHERE turn_id = %s",
                (debut_s - index, debut_s - index - duree, turn_id))

    def test_tours_courts_en_rafale(self):
        self._tours("orch", [30.0] * 13 + [900.0, 600.0])
        self._tours("dev1", [20.0] * 3)
        self._tours("dev2", [30.0] * 20, debut_s=7200.0)  # hors de la dernière heure
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "turn_churn"]
        self.assertEqual([(a["agent"], a["value"], a["turns"]) for a in alertes],
                         [("orch", 13, 15)])
        self.assertEqual(alertes[0]["threshold"], exploitation.DEFAULT_TURN_CHURN)
        self.assertIn("13 tour(s) de moins de 120s", alertes[0]["detail"])
        # seuils réglables ; 0 = désactivée
        self.assertFalse([a for a in exploitation.alerts(self.cfg, self.db, turn_churn=14)
                          if a["type"] == "turn_churn"])
        self.assertFalse([a for a in exploitation.alerts(self.cfg, self.db, turn_churn=0)
                          if a["type"] == "turn_churn"])
        courts = [a["agent"] for a in exploitation.alerts(
            self.cfg, self.db, turn_churn=3, short_turn_s=25.0) if a["type"] == "turn_churn"]
        self.assertEqual(courts, ["dev1"])

    def test_options_partagees_par_alerts_et_notify(self):
        parser = argparse.ArgumentParser()
        with mock.patch.dict(os.environ, {"AMEESH_ALERT_TURN_CHURN": "20"}):
            exploitation.add_threshold_arguments(parser)
        seuils = exploitation.thresholds(parser.parse_args(["--short-turn", "90"]))
        self.assertEqual((seuils["turn_churn"], seuils["short_turn_s"]), (20.0, 90.0))


class AmeeshSetTest(_Base):
    def test_mail_batch_par_agent(self):
        _runner, worker = self._worker("orch-s")
        proc = self.mesh("set", "orch-s", "mail_batch=2m")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("regroupement=120s", proc.stdout)
        self.assertEqual(worker.mail_batch(), 120.0)
        proc = self.mesh("set", "orch-s", "mail_batch=0")
        self.assertIn("regroupement=immédiat", proc.stdout)
        self.assertEqual(worker.mail_batch(), 0.0)
        proc = self.mesh("set", "orch-s", "mail_batch=")
        self.assertIn("regroupement=immédiat (défaut)", proc.stdout)  # banc : 0
        self.assertEqual(worker.mail_batch(), worker.runner.mail_batch)
        self.assertEqual(self.mesh("set", "orch-s", "mail_batch=bientot").returncode, 2)


if __name__ == "__main__":
    unittest.main()
