# SPDX-License-Identifier: AGPL-3.0-only
"""Courrier en souffrance (correctif du 2026-10-11).

Constat : 42 messages jamais livrés, sans qu'aucun expéditeur le sache — 27
adressés à « orchestrator » (faute de frappe, l'envoi avait créé un agent
fantôme), 15 à un agent arrêté à la main dont l'ancienne session écrivait
encore sous son nom. Ces tests couvrent les cinq parades : refus d'un nom
inconnu (noms proches), refus d'un destinataire arrêté (sauf `--queue`),
refus d'une identité d'expéditeur arrêtée, alerte `mail_undeliverable` (et
`ameesh projects`), renvoi `ameesh mail forward`.
"""
from __future__ import annotations
from unittest import mock

import dataclasses
import json
import os
import shlex
import threading
import time
import unittest
from contextlib import contextmanager

from ameesh import (exploitation, fastmail, mail, notify, projects, registry, storage,
                    undeliverable)
from ameesh.backend import PgBackend
from ameesh.scoped_db import ScopedDb

from .support import PgTestCase

NOW = 1_790_000_000.0


class FonctionsTest(unittest.TestCase):
    """Sans base : distance, rôles, noms proches, regroupement, courriers."""

    def test_distance_d_edition(self):
        d = undeliverable.distance
        self.assertEqual(d("orchestrator", "orchestrateur"), 2)
        self.assertEqual(d("cluade3", "claude3"), 1)          # transposition
        self.assertEqual(d("claude33", "claude3"), 1)
        self.assertEqual(d("Claude3", "claude3"), 0)          # casse ignorée
        self.assertEqual(d("", "abc"), 3)
        self.assertEqual(d("dev1", "dev2"), 1)

    def test_role(self):
        for name in ("orchestrator", "orchestrateur", "Orchestrateurs", "orch"):
            self.assertEqual(undeliverable.role_of(name), "orchestrateur", name)
        for name in ("orc", "claude1", "chef"):
            self.assertIsNone(undeliverable.role_of(name), name)

    def test_identite_d_emission_de_la_veille(self):
        # #3342 : `watch.SENDER` n'a pas de lecteur ; tout autre nom reste un agent
        self.assertTrue(undeliverable.watch_sender("veille"))
        self.assertFalse(undeliverable.watch_sender("mesh-design"))
        text = undeliverable.watch_sender_text("veille")
        self.assertIn("nobody reads its mailbox", text)
        self.assertIn("ameesh fil", text)

    def test_noms_proches(self):
        rows = [{"name": n, "team": t, "status": s} for n, t, s in (
            ("orchestrateur", "manaty", "idle"), ("claude1", "manaty", "idle"),
            ("claude2", "manaty", "running"), ("claude3", "manaty", "stopped"),
            ("deepseek1", "manaty", "idle"), ("dev", "", "idle"))]
        near = undeliverable.suggestions("orchestrator", rows, role_names=["orchestrateur"],
                                         team="manaty")
        self.assertEqual(near, [("orchestrateur", "orchestrator of team manaty")])
        near = undeliverable.suggestions("claude33", rows)
        self.assertEqual(near[0], ("claude3", "1 letter apart, stopped"))  # le plus proche
        self.assertEqual(near[1], ("claude1", "2 letters apart"))
        near = [n for n, _ in undeliverable.suggestions("deep", rows)]
        self.assertEqual(near, ["deepseek1"])                  # même début
        self.assertEqual(undeliverable.suggestions("mesh", [{"name": "mesh-design"}]),
                         [("mesh-design", "same prefix")])
        self.assertEqual(undeliverable.suggestions("zzzzzz", rows), [])
        # nom court : une lettre au plus
        self.assertEqual([n for n, _ in undeliverable.suggestions("dvx", rows)], [])
        self.assertLessEqual(len(undeliverable.suggestions("d", rows + [
            {"name": "d%d" % i} for i in range(9)])), undeliverable.MAX_SUGGESTIONS)

    def test_orchestrateurs_de_l_equipe(self):
        rows = [{"name": "orch-a", "team": "a"}, {"name": "orch-b", "team": "b"},
                {"name": "orch-libre", "team": ""},
                {"name": "orch-arrete", "team": "a", "status": "stopped"}]
        known = ["orch-a", "orch-b", "orch-libre", "orch-arrete"]
        self.assertEqual(undeliverable.team_orchestrators(rows, known, {"a"}),
                         ["orch-a", "orch-libre"])
        self.assertEqual(undeliverable.team_orchestrators(rows, known, ()),
                         ["orch-a", "orch-b", "orch-libre"])
        self.assertEqual(undeliverable.team_orchestrators(rows, known, {"a"},
                                                          exclude=("orch-a",)),
                         ["orch-libre"])

    def test_regroupement_par_destinataire(self):
        groups = undeliverable.by_recipient([
            {"recipient": "x", "sender": "a", "n": 2, "oldest_ts": 10.0, "newest_ts": 20.0,
             "unknown": True},
            {"recipient": "x", "sender": "b", "n": 5, "oldest_ts": 5.0, "newest_ts": 15.0,
             "unknown": True},
            {"recipient": "y", "sender": "a", "n": 1, "oldest_ts": 7.0, "newest_ts": 7.0,
             "unknown": False}])
        self.assertEqual([(g["recipient"], g["count"], g["oldest_ts"], g["newest_ts"])
                          for g in groups], [("x", 7, 5.0, 20.0), ("y", 1, 7.0, 7.0)])
        self.assertEqual(groups[0]["senders"], [("b", 5), ("a", 2)])

    def test_courrier_aux_orchestrateurs(self):
        alerte = {"type": "mail_undeliverable", "agent": "claude3", "value": 15,
                  "threshold": 900, "reason": "arrete", "senders": ["claude1", "claude2"],
                  "successor": "mesh-design", "orchestrators": ["orchestrateur", "claude3"]}
        briefs = undeliverable.orchestrator_briefs(alerte)
        self.assertEqual(list(briefs), ["orchestrateur"])     # jamais à la boîte morte
        self.assertIn("15 message(s) addressed to claude3", briefs["orchestrateur"])
        self.assertIn("ameesh mail forward claude3 mesh-design", briefs["orchestrateur"])
        alerte.update(agent="orchestrator", reason="inconnu", suggestions=["orchestrateur"])
        text = undeliverable.orchestrator_briefs(alerte)["orchestrateur"]
        self.assertIn('"orchestrator", not in the registry', text)
        self.assertIn("Close names: orchestrateur", text)
        self.assertEqual(undeliverable.orchestrator_briefs({"type": "idle_capacity"}), {})

    def test_consigne_et_hook_disent_le_renvoi(self):
        row = {"id": 7, "sender": "claude1", "created_ts": NOW, "body": "SHA abc123 fusionné",
               "kind": "notify", "payload": {}, "forwarded_from": "claude3"}
        texte, _retenus, _reste = mail.prompt_for(None, [row], kind="mail")
        self.assertIn("[forwarded: first addressed to claude3", texte)
        from ameesh import cli
        rendu = cli.render([mail.normalize(row)])
        self.assertIn("[forwarded: first addressed to claude3", rendu)
        # un message ordinaire : rendu inchangé (JSON des hooks identique à la v0)
        self.assertNotIn("[forwarded", cli.render([mail.normalize(dict(row, forwarded_from=None))]))

    def test_vue_par_projet(self):
        board = {
            "agents": [{"name": "claude1", "team": "manaty", "status": "idle"},
                       {"name": "claude3", "team": "manaty", "status": "stopped",
                        "stop_reason": "manuel"},
                       {"name": "w1", "team": "autre", "status": "idle"}],
            "lots": [],
            "dead_letters": [
                {"recipient": "claude3", "sender": "claude1", "n": 15,
                 "oldest_ts": NOW - 7200, "unknown": False},
                {"recipient": "orchestrator", "sender": "claude1", "n": 27,
                 "oldest_ts": NOW - 3600, "unknown": True},
                {"recipient": "orchestrator", "sender": "w1", "n": 2,
                 "oldest_ts": NOW - 60, "unknown": True}]}
        view = projects.build(board, now=NOW, paid_harnesses=())
        by = {p["name"]: p for p in view["projects"]}
        self.assertEqual(by["manaty"]["undeliverable"], 42)
        self.assertEqual([(e["recipient"], e["why"], e["count"], e["senders"])
                          for e in by["manaty"]["undeliverable_mail"]],
                         [("claude3", "stopped", 15, ["claude1"]),
                          ("orchestrator", "unknown", 27, ["claude1"])])
        self.assertEqual(by["autre"]["undeliverable"], 2)       # le projet de l'expéditeur
        self.assertEqual(view["undeliverable"], 44)
        self.assertTrue(by["autre"]["active"])
        text = projects.format_text(view, 120)
        self.assertIn("42 undeliverable", text)
        self.assertIn("! 42 undeliverable message(s): claude3 (stopped) 15, orchestrator "
                      "(unknown) 27", text)
        # sans courrier en souffrance : rien ne change
        view = projects.build(dict(board, dead_letters=[]), now=NOW, paid_harnesses=())
        self.assertEqual(view["undeliverable"], 0)
        self.assertNotIn("undeliverable", projects.format_text(view, 120))


class _Base(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")

    def agent(self, name, team="manaty", host=None, **sql):
        registry.upsert(self.db, name, harness="claude", host=host or self.cfg.host,
                        chantier=team)
        sets = dict(team=team, **sql)
        self.db.execute("UPDATE agent_registry SET %s WHERE name = %%s" % ", ".join(
            "%s = %%s" % key for key in sets), tuple(sets.values()) + (name,))

    def stop(self, name, reason="manuel", text="arrêté à la main"):
        registry.set_status(self.db, name, "stopped", status_text=text, stop_reason=reason)

    def pending(self, name):
        return [m["body"] for m in mail.unread(self.db, name)]

    def send(self, *args, env=None, **extra):
        return self.cli("send", *args, env=env or self.env(**extra))


class EnvoiRefuseTest(_Base):
    """`agent-mail send` : nom inconnu, destinataire arrêté, expéditeur arrêté."""

    def setUp(self) -> None:
        super().setUp()
        for name in ("orchestrateur", "claude1", "claude2", "mesh-design"):
            self.agent(name)
        self.agent("claude3", responsible="human:proprio")
        self.agent("chef-b", team="beta")

    def test_nom_inconnu_refuse_sans_creer_d_agent(self):
        proc = self.send("orchestrator", "Rapport : lot 12 fusionné (SHA abc123).",
                         AGENT_MAIL_NAME="claude1",
                         AMEESH_ALERT_ORCHESTRATORS="orchestrateur,chef-b")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn('message not delivered — "orchestrator" is not in the registry',
                      proc.stderr)
        # le rôle : l'orchestrateur de l'équipe de l'expéditeur, pas celui d'une autre
        self.assertIn("close names: orchestrateur (orchestrator of team manaty)",
                      proc.stderr)
        self.assertNotIn("chef-b", proc.stderr)
        self.assertIsNone(registry.get(self.db, "orchestrator"))   # aucun fantôme
        self.assertEqual(self.pending("orchestrator"), [])
        # distance d'édition, sans rôle déclaré
        proc = self.send("claude33", "Bonjour.", AGENT_MAIL_NAME="claude1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("claude3 (1 letter apart)", proc.stderr)
        self.assertIsNone(registry.get(self.db, "claude33"))
        # « all » reste une destination ; un nom illisible reste invalide
        proc = self.send("all", "Point d'équipe.", AGENT_MAIL_NAME="claude1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.send("human:proprio", "x", AGENT_MAIL_NAME="claude1").returncode,
                         2)

    def test_courrier_a_l_identite_de_veille_refuse(self):
        """#3342 : `veille` n'ÉMET que la consigne de veille ; les comptes rendus
        des agents réveillés s'y perdaient (221 non-lus, 13,6 j). Refus avant
        dépôt, avec le geste utile ; `--queue` ne range pas dans une boîte morte,
        et une copie ne contourne pas le refus."""
        self.agent("veille", team="", harness="other", mode="externe")
        proc = self.send("veille", "Veille 12:30 faite (mesh-design), rien à signaler.",
                         AGENT_MAIL_NAME="claude1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("message not delivered — veille is the sender of the "
                      "`ameesh watch` instruction", proc.stderr)
        self.assertIn("nobody reads its mailbox", proc.stderr)
        self.assertIn("ameesh fil", proc.stderr)
        self.assertEqual(self.pending("veille"), [])
        proc = self.send("veille", "Compte rendu.", "--queue", AGENT_MAIL_NAME="claude1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(self.pending("veille"), [])
        # une copie non plus : elle partirait dans la même boîte sans lecteur
        proc = self.send("orchestrateur", "Point d'équipe.",
                         "--cc", "veille", AGENT_MAIL_NAME="claude1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("copy refused", proc.stderr)
        self.assertEqual(self.pending("veille"), [])
        self.assertEqual(self.pending("orchestrateur"), [])

    def test_destinataire_non_reveillable_le_message_part_avec_avertissement(self):
        """#858 : agent inscrit mais non réveillable à l'instant (profil divergé,
        en attente du prochain canon sync) : le message est déposé, le réveil
        est différé — l'expéditeur doit le savoir."""
        self.db.execute("UPDATE agent_registry SET canon_ref = 'agents/claude2.md', "
                        "placement_ok = true, placement_profile = 'ancien' "
                        "WHERE name = 'claude2'")
        proc = self.send("claude2", "Relecture demandée sur ce lot.",
                         AGENT_MAIL_NAME="claude1")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("delivered to claude2", proc.stdout)
        self.assertIn("warning: claude2 is not wakeable right now", proc.stderr)
        self.assertIn("profile diverged", proc.stderr)
        self.assertEqual(self.pending("claude2"), ["Relecture demandée sur ce lot."])
        # une session EXTERNE n'est pas réveillée par construction : pas d'alerte
        self.agent("coord", mode="externe")
        proc = self.send("coord", "Pour information.", AGENT_MAIL_NAME="claude1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("not wakeable", proc.stderr)
        self.assertEqual(self.pending("coord"), ["Pour information."])

    def test_un_envoi_ne_touche_pas_le_registre_du_destinataire(self):
        self.agent("ailleurs", host="autre-hote")
        before = registry.get(self.db, "ailleurs")
        proc = self.send("ailleurs", "Une question.", AGENT_MAIL_NAME="claude1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = registry.get(self.db, "ailleurs")
        # avant : l'envoi réinscrivait le destinataire sur l'hôte de l'expéditeur
        self.assertEqual(after["host"], "autre-hote")
        self.assertEqual(after["last_seen_ts"], before["last_seen_ts"])
        self.assertEqual(self.pending("ailleurs"), ["Une question."])

    def test_destinataire_arrete_refuse_sauf_queue(self):
        registry.set_session(self.db, "claude3", "s-claude3")
        storage.of(self.db).session_bindings.bind(
            host=self.cfg.host, harness="claude", session_id="s-claude3", agent="mesh-design",
            pid=None, created_by="human:proprio")
        self.stop("claude3")
        proc = self.send("claude3", "Verdict : ok, gèle.", AGENT_MAIL_NAME="claude1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("message not delivered — claude3 is stopped (since ", proc.stderr)
        self.assertIn("reason: manuel — arrêté à la main", proc.stderr)
        self.assertIn("responsible: human:proprio", proc.stderr)
        self.assertIn("its work was taken over by mesh-design", proc.stderr)
        self.assertIn("--queue", proc.stderr)
        self.assertEqual(self.pending("claude3"), [])
        proc = self.send("claude3", "Pour ta reprise.", "--queue", AGENT_MAIL_NAME="claude1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("delivered to claude3", proc.stdout)
        self.assertIn("warning: claude3 is stopped", proc.stderr)
        self.assertEqual(self.pending("claude3"), ["Pour ta reprise."])

    def test_en_pause_au_repos_ou_mort_reste_accepte(self):
        registry.set_status(self.db, "claude2", "blocked", status_text="budget horaire")
        registry.set_status(self.db, "mesh-design", "dead", status_text="bail expiré",
                            stop_reason="bail_expire")
        for name in ("claude2", "mesh-design", "orchestrateur"):
            proc = self.send(name, "Bonjour %s." % name, AGENT_MAIL_NAME="claude1")
            self.assertEqual(proc.returncode, 0, (name, proc.stderr))
            self.assertEqual(self.pending(name), ["Bonjour %s." % name])

    def test_expediteur_arrete_refuse(self):
        self.stop("claude3")
        # --from : l'identité liée à la session est donnée
        proc = self.send("orchestrateur", "Rapport.", "--from", "claude3",
                         AGENT_MAIL_NAME="mesh-design")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("sender claude3 is stopped", proc.stderr)
        self.assertIn("identity bound to this session (agent-mail whoami): mesh-design",
                      proc.stderr)
        self.assertIn("resend without --from", proc.stderr)
        # identité résolue de la session (AGENT_MAIL_NAME) : refusée aussi
        proc = self.send("orchestrateur", "Rapport.", AGENT_MAIL_NAME="claude3")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("this session speaks on behalf of claude3", proc.stderr)
        # identité liée au bail d'un agent arrêté : la raison est dite
        proc = self.send("orchestrateur", "Rapport.", AGENT_MAIL_NAME="claude3",
                         AMEESH_RUNNER_ID="runner-x", AMEESH_LEASE_EPOCH="1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unbound sender (claude3: ", proc.stderr)
        self.assertEqual(self.pending("orchestrateur"), [])

    def test_session_liee_a_un_agent_arrete(self):
        env = self.env()
        env.pop("AGENT_MAIL_NAME", None)
        proc = self.cli("bind", "claude3", "--session", "s-1", "--harness", "claude", "--pid",
                        str(os.getpid()), "--force", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.stop("claude3")
        proc = self.send("orchestrateur", "Rapport.", env=env)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("this session is bound to claude3", proc.stderr)
        self.assertIn("agent-mail unbind --session s-1 --harness claude", proc.stderr)

    def test_diffusion_ecarte_les_agents_arretes(self):
        self.stop("claude3")
        proc = self.send("all", "Point d'équipe à 15 h.", AGENT_MAIL_NAME="orchestrateur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("claude3", proc.stdout)
        self.assertIn("not delivered to claude3 (stopped", proc.stderr)
        self.assertEqual(self.pending("claude3"), [])
        self.assertEqual(self.pending("claude1"), ["Point d'équipe à 15 h."])
        skipped: list = []
        targets = PgBackend(self.cfg, self.db).send("orchestrateur", "all", "À tous.",
                                                    include_stopped=True, skipped=skipped)
        self.assertIn("claude3", targets)
        self.assertEqual(skipped, [])


class AlerteTest(_Base):
    """`mail_undeliverable` : levée, routage, courrier aux orchestrateurs."""

    def setUp(self) -> None:
        super().setUp()
        for name in ("orchestrateur", "claude1", "mesh-design"):
            self.agent(name, responsible="human:proprio")
        self.agent("claude3", responsible="human:resp3")
        self.agent("chef-b", team="beta")
        # #3036 : un brief n'est plus déposé à un agent sans bail vivant.
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = 'runner-' || name,"
            " lease_expires_at = now() + interval '1 hour'"
            " WHERE name IN ('orchestrateur', 'claude1', 'mesh-design', 'chef-b')")

    def vieillir(self, minutes=20):
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - make_interval("
                        "mins => %s)", (int(minutes),))

    def alertes(self, **kw):
        kw.setdefault("orchestrators_declared", ("orchestrateur", "chef-b"))
        return [a for a in exploitation.alerts(self.cfg, self.db, **kw)
                if a["type"] == "mail_undeliverable"]

    def test_agent_arrete_et_nom_inconnu(self):
        registry.set_session(self.db, "claude3", "s-claude3")
        storage.of(self.db).session_bindings.bind(
            host=self.cfg.host, harness="claude", session_id="s-claude3", agent="mesh-design",
            pid=None, created_by="human:proprio")
        self.stop("claude3")
        for n in range(3):
            mail.send(self.db, "claude1", "claude3", "Réponse %d." % n)
        mail.send(self.db, "claude1", "orchestrator", "Rapport (faute de nom).")
        mail.send(self.db, "orchestrateur", "claude1", "Pour toi, vivant.")
        self.assertEqual(self.alertes(), [])                     # moins de 15 min
        self.vieillir()
        by = {a["agent"]: a for a in self.alertes()}
        self.assertEqual(set(by), {"claude3", "orchestrator"})
        arrete = by["claude3"]
        self.assertEqual((arrete["reason"], arrete["value"], arrete["threshold"]),
                         ("arrete", 3, 900.0))
        self.assertEqual(arrete["responsible"], "human:resp3")
        self.assertEqual(arrete["orchestrators"], ["orchestrateur"])  # pas chef-b (beta)
        self.assertEqual(arrete["successor"], "mesh-design")
        self.assertIn("ameesh mail forward claude3 mesh-design", arrete["detail"])
        inconnu = by["orchestrator"]
        self.assertEqual((inconnu["reason"], inconnu["value"]), ("inconnu", 1))
        self.assertEqual(inconnu["responsible"], "human:proprio")   # celui des expéditeurs
        self.assertEqual(inconnu["suggestions"][0], "orchestrateur")
        self.assertEqual(inconnu["orchestrators"], ["orchestrateur"])
        # seuil réglable, et 0 coupe l'alerte
        self.assertEqual(self.alertes(mail_undeliverable_s=3600), [])
        self.assertEqual(self.alertes(mail_undeliverable_s=0), [])
        # clé de dédoublonnage : une alerte par destinataire
        self.assertEqual(len({exploitation.alert_key(a) for a in by.values()}), 2)
        self.assertIn("mail_undeliverable", notify.DEFAULT_TYPES)
        self.assertIn("mail_undeliverable", notify.URGENT_TYPES)
        # l'agent relancé : plus d'alerte pour lui
        registry.set_status(self.db, "claude3", "idle")
        self.assertEqual([a["agent"] for a in self.alertes()], ["orchestrator"])

    def test_humain_declare_n_est_pas_une_boite_morte(self):
        """#4519 : un humain DÉCLARÉ (`human:<id>`) lit par `ameesh decide` et
        par le fil ; son courrier n'est ni alerté « non distribuable » ni
        expiré. Un `human:<id>` non déclaré reste un nom inconnu."""
        cfg = dataclasses.replace(self.cfg, humans="smichea")
        mail.send(self.db, "claude1", "human:smichea",
                  "Passe de cohérence du backlog — ameesh.", kind="event")
        mail.send(self.db, "claude1", "human:inconnu", "Rapport.", kind="event")
        self.vieillir()
        alertes = [a for a in exploitation.alerts(
            cfg, self.db, orchestrators_declared=("orchestrateur",))
            if a["type"] == "mail_undeliverable"]
        self.assertEqual([a["agent"] for a in alertes], ["human:inconnu"])
        self.assertEqual((alertes[0]["reason"], alertes[0]["value"]), ("inconnu", 1))
        # le ménage de l'exécuteur n'expire pas le courrier de l'humain déclaré
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '8 days'")
        report = undeliverable.sweep(cfg, self.db, days=7)
        self.assertEqual([r["recipient"] for r in report["expired"]], ["human:inconnu"])
        self.assertEqual(len(mail.unread(self.db, "human:smichea")), 1)

    def test_vue_projets_ignore_le_courrier_d_un_humain_declare(self):
        """#4519 : `ameesh projects` (et la vue `snapshot`) ne compte pas le
        courrier d'un humain déclaré comme non distribuable."""
        mail.send(self.db, "claude1", "human:smichea",
                  "Passe de cohérence du backlog — ameesh.", kind="event")
        self.vieillir()
        db = self.connect(humans="smichea")
        self.addCleanup(db.close)
        view = projects.snapshot(db)
        self.assertEqual(view["undeliverable"], 0)
        self.assertEqual([p["name"] for p in view["projects"]
                          if p["undeliverable_mail"]], [])

    def test_notify_previent_les_orchestrateurs(self):
        self.stop("claude3")
        mail.send(self.db, "claude1", "claude3", "Verdict : ok.")
        self.vieillir()
        logs: list = []
        notifier = notify.Notifier(self.cfg, notify.parse_config({}),
                                   sender=notify.DrySender(), log=logs.append)
        notifier.run_pass(self.db, thresholds={
            "orchestrators_declared": ("orchestrateur",)})
        [brief] = [m for m in mail.unread(self.db, "orchestrateur")]
        self.assertEqual((brief["sender"], brief["kind"]), ("ameesh", "event"))
        self.assertIn("Undeliverable mail: 1 message(s) addressed to claude3",
                      brief["body"])
        self.assertIn("ameesh mail forward claude3", brief["body"])
        payload = mail.message_payload(brief)
        self.assertEqual((payload["alert"], payload["recipient"], payload["reason"]),
                         ("mail_undeliverable", "claude3", "arrete"))
        self.assertEqual(self.pending("claude3"), ["Verdict : ok."])   # rien n'est déplacé
        # une alerte qui dure : pas de second courrier
        notifier.run_pass(self.db, thresholds={"orchestrators_declared": ("orchestrateur",)})
        self.assertEqual(len(mail.unread(self.db, "orchestrateur")), 1)

    def test_vue_par_projet_en_une_requete(self):
        self.stop("claude3")
        mail.send(self.db, "claude1", "claude3", "Réponse.")
        mail.send(self.db, "claude1", "orchestrator", "Rapport.")
        calls = []
        real = self.db.query

        def counting(sql, params=()):
            calls.append(sql)
            return real(sql, params)

        self.db.query = counting
        try:
            view = projects.snapshot(self.db, paid_harnesses=())
        finally:
            del self.db.query
        self.assertEqual(len(calls), 1)
        manaty = next(p for p in view["projects"] if p["name"] == "manaty")
        self.assertEqual(manaty["undeliverable"], 2)
        self.assertEqual({e["recipient"]: e["why"] for e in manaty["undeliverable_mail"]},
                         {"claude3": "stopped", "orchestrator": "unknown"})
        out = self.mesh("projects")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("2 undeliverable message(s)", out.stdout)


class RenvoiTest(_Base):
    """`ameesh mail forward <ancien> <nouveau>` : vider une boîte morte."""

    def setUp(self) -> None:
        super().setUp()
        for name in ("claude1", "mesh-design", "orchestrateur"):
            self.agent(name)
        self.agent("claude3")

    def test_legacy_runner_label_forwarded_by_agent_and_alert_cleared(self):
        old = "runner runner-mty-amh-impl-08@ameesh-agents-2"
        mail.send(self.db, "claude1", old, "Closure withheld.")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '20 minutes'")
        [alert] = undeliverable.alerts(
            self.cfg, self.db, registry.overview(self.db), time.time(),
            orchestrators_declared=("orchestrateur",))
        command = "ameesh mail forward %s <agent>" % shlex.quote(old)
        self.assertIn(command, alert["detail"])
        self.assertIn(command, undeliverable.brief_text(alert))
        for flags in (("--dry-run",), ()):
            proc = self.mesh("mail", "forward", old, "mesh-design", *flags,
                             env=self.env(AGENT_MAIL_NAME="orchestrateur"))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            if flags:
                self.assertEqual(len(mail.unread(self.db, old)), 1)
                self.assertEqual(mail.unread(self.db, "mesh-design"), [])
        self.assertEqual(mail.unread(self.db, old), [])
        [copy] = mail.unread(self.db, "mesh-design")
        self.assertEqual(copy["forwarded_from"], old)
        self.assertEqual(copy["sender"], "claude1")
        self.assertEqual(undeliverable.alerts(
            self.cfg, self.db, registry.overview(self.db), time.time(),
            orchestrators_declared=("orchestrateur",)), [])

    def test_renvoi_garde_expediteur_et_date(self):
        self.stop("claude3")
        first = mail.send(self.db, "claude1", "claude3", "Rapport : SHA abc123.",
                          work_item_id="12")
        second = mail.send(self.db, "orchestrateur", "claude3", "Gel du lot 12.",
                           kind="event", payload={"urgent": True})
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '2 hours'")
        avant = {m["id"]: m for m in mail.unread(self.db, "claude3")}
        # à blanc : rien ne bouge
        proc = self.mesh("mail", "forward", "claude3", "mesh-design", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dry run: 2 pending message(s) for claude3 (stopped)", proc.stdout)
        self.assertEqual(len(mail.unread(self.db, "claude3")), 2)
        # #1689 (N1) : le renvoi dépose sans passer par `mail.send` ; le
        # marqueur local du destinataire doit dire « à vérifier » sans TTL.
        fastmail.note(self.cfg, "mesh-design", pending=False, unread=0)
        proc = self.mesh("mail", "forward", "claude3", "mesh-design",
                         env=self.env(AGENT_MAIL_NAME="orchestrateur"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2 message(s) re-delivered from claude3 (stopped) to mesh-design", proc.stdout)
        self.assertTrue(fastmail.read_marker(self.cfg, "mesh-design")["pending"])
        self.assertEqual(mail.unread(self.db, "claude3"), [])
        copies = mail.unread(self.db, "mesh-design")
        self.assertEqual([(c["sender"], c["body"], c["kind"]) for c in copies],
                         [("claude1", "Rapport : SHA abc123.", "notify"),
                          ("orchestrateur", "Gel du lot 12.", "event")])
        for copy, original in zip(copies, (first, second)):
            self.assertEqual(copy["created_ts"], avant[original]["created_ts"])  # date gardée
            self.assertEqual(copy["forwarded_from"], "claude3")
        self.assertTrue(mail.is_urgent(copies[1]))
        self.assertEqual(self.db.query("SELECT work_item_id FROM agent_mailbox WHERE id = %s",
                                       (copies[0]["id"],))[0]["work_item_id"], "12")
        # l'original n'est plus en attente, et dit où il est parti
        rows = {r["id"]: r for r in self.db.query(
            "SELECT id, status, delivered_at IS NOT NULL AS done, meta->'forwarded' AS fwd"
            " FROM agent_mailbox WHERE recipient = 'claude3'")}
        for original, copy in zip((first, second), copies):
            self.assertEqual((rows[original]["status"], rows[original]["done"]),
                             ("delivered", True))
            fwd = rows[original]["fwd"]
            fwd = json.loads(fwd) if isinstance(fwd, str) else fwd
            self.assertEqual((fwd["to"], fwd["message_id"], fwd["by"]),
                             ("mesh-design", copy["id"], "orchestrateur"))
        # le destinataire voit le renvoi ; le fil le note
        hook = self.cli("hook", "claude", env=self.env(AGENT_MAIL_NAME="mesh-design"),
                        stdin=json.dumps({"hook_event_name": "SessionStart",
                                          "cwd": self.tmp}))
        contexte = json.loads(hook.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("— from claude1 at ", contexte)
        self.assertIn("[forwarded: first addressed to claude3", contexte)
        fil = self.mesh("fil", "show", "manaty")
        self.assertIn("Forwarded 2 undeliverable message(s): the mailbox of claude3", fil.stdout)
        # plus rien à renvoyer
        proc = self.mesh("mail", "forward", "claude3", "mesh-design")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("nothing to forward", proc.stdout)

    def test_boite_d_un_nom_inconnu_et_refus(self):
        mail.send(self.db, "claude1", "orchestrator", "Rapport (faute de nom).")
        # nom inconnu : renvoyé
        moved = undeliverable.forward(self.cfg, self.db, "orchestrator", "orchestrateur",
                                      actor="human:proprio")
        self.assertEqual(len(moved["forwarded"]), 1)
        self.assertEqual(moved["old_state"], "absent du registre")
        self.assertEqual(self.pending("orchestrateur"), ["Rapport (faute de nom)."])
        # un agent vivant garde sa boîte
        mail.send(self.db, "claude1", "claude3", "Pour toi.")
        proc = self.mesh("mail", "forward", "claude3", "mesh-design")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("forward refused — claude3 is not stopped", proc.stderr)
        self.assertIn("agent-runner stop claude3", proc.stderr)
        self.assertEqual(self.pending("claude3"), ["Pour toi."])
        self.stop("claude3")
        # vers un nom inconnu ou un agent arrêté : refusé
        with self.assertRaises(undeliverable.Refused) as ctx:
            undeliverable.forward(self.cfg, self.db, "claude3", "mesh-desing", actor="x")
        self.assertIn("mesh-design (1 letter apart)", str(ctx.exception))
        self.stop("mesh-design")
        with self.assertRaises(undeliverable.Refused):
            undeliverable.forward(self.cfg, self.db, "claude3", "mesh-design", actor="x")
        with self.assertRaises(undeliverable.Refused):
            undeliverable.forward(self.cfg, self.db, "claude3", "claude3", actor="x")
        self.assertEqual(self.pending("claude3"), ["Pour toi."])
        self.assertEqual(self.mesh("mail", "forward", "claude3").returncode, 2)

    def test_signature_et_reservation_active(self):
        self.stop("claude3")
        signed = mail.send(self.db, "claude1", "claude3", "Message signé.",
                           signature="c2ln", signature_key="cle", signed_payload="{}",
                           nonce="n-%d" % time.time_ns())
        reserved = mail.send(self.db, "claude1", "claude3", "En cours de remise.")
        self.db.execute(
            "UPDATE agent_mailbox SET meta = jsonb_build_object('consigne', jsonb_build_object("
            "'jeton', 'j', 'expire', extract(epoch from clock_timestamp()) + 600))"
            " WHERE id = %s", (reserved,))
        result = undeliverable.forward(self.cfg, self.db, "claude3", "mesh-design",
                                       actor="human:proprio")
        self.assertEqual([r["original_id"] for r in result["forwarded"]], [signed])
        [copy] = mail.unread(self.db, "mesh-design")
        # la signature couvre le premier destinataire : elle ne suit pas
        self.assertEqual((copy["signature"], copy["nonce"]), (None, None))
        self.assertEqual([m["id"] for m in mail.unread(self.db, "claude3")], [reserved])


class SweepTest(_Base):
    """L976 : le balayage vide les boîtes mortes sans geste humain — re-livraison
    au successeur déclaré (session liée, L41), sinon expiration message par
    message après `mail_dead_letter_days`, avec trace ; l'alerte
    `mail_undeliverable` peut alors se vider d'elle-même."""

    def setUp(self) -> None:
        super().setUp()
        for name in ("claude1", "mesh-design", "orchestrateur"):
            self.agent(name)
        self.agent("claude3")

    def lie(self, agent="claude3", session="s-claude3", to="mesh-design"):
        """Déclare `to` successeur de `agent` (sa session est liée)."""
        registry.set_session(self.db, agent, session)
        storage.of(self.db).session_bindings.bind(
            host=self.cfg.host, harness="claude", session_id=session, agent=to,
            pid=None, created_by="human:proprio")

    def age(self, days):
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - make_interval("
                        "days => %s)", (int(days),))

    def alertes(self):
        return [a for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == "mail_undeliverable"]

    def test_re_delivre_au_successeur_declare(self):
        self.lie()
        self.stop("claude3")
        mail.send(self.db, "claude1", "claude3", "Rapport 1.")
        mail.send(self.db, "orchestrateur", "claude3", "Gel du lot.", kind="event")
        self.age(2)
        report = undeliverable.sweep(self.cfg, self.db)
        self.assertEqual(report["days"], 7)
        [fwd] = report["forwarded"]
        self.assertEqual((fwd["old"], fwd["new"], fwd["count"]),
                         ("claude3", "mesh-design", 2))
        self.assertEqual(report["expired"], [])
        self.assertEqual(self.pending("claude3"), [])
        copies = mail.unread(self.db, "mesh-design")
        self.assertEqual([c["body"] for c in copies], ["Rapport 1.", "Gel du lot."])
        self.assertEqual(copies[0]["forwarded_from"], "claude3")   # expéditeur et date gardés
        self.assertEqual(self.alertes(), [])
        # rejoué : plus rien à faire, aucune erreur
        again = undeliverable.sweep(self.cfg, self.db)
        self.assertEqual((again["forwarded"], again["expired"], again["refused"]),
                         ([], [], []))

    def test_expire_apres_la_retention_avec_trace(self):
        self.stop("claude3")
        mail.send(self.db, "claude1", "claude3", "Rapport 1.")
        mail.send(self.db, "claude1", "claude3", "Rapport 2.")
        self.age(3)
        report = undeliverable.sweep(self.cfg, self.db)
        self.assertEqual((report["forwarded"], report["expired"]), ([], []))
        self.assertEqual(len(self.pending("claude3")), 2)          # sous la rétention
        self.assertEqual(len(self.alertes()), 1)                   # mais déjà en alerte
        self.age(8)
        report = undeliverable.sweep(self.cfg, self.db)
        [exp] = report["expired"]
        self.assertEqual((exp["recipient"], exp["count"], exp["unknown"]),
                         ("claude3", 2, False))
        self.assertEqual(self.pending("claude3"), [])
        rows = self.db.query("SELECT meta->'expired' AS note FROM agent_mailbox"
                             " WHERE recipient = 'claude3' ORDER BY id")
        for row in rows:
            note = json.loads(row["note"]) if isinstance(row["note"], str) else row["note"]
            self.assertEqual((note["days"], note["by"]), (7, "ameesh:mail-sweep"))
        self.assertIn("Expired 2 undeliverable message(s) addressed to claude3",
                      self.mesh("fil", "show", "manaty").stdout)
        self.assertEqual(self.alertes(), [])                       # l'alerte se vide

    def test_expire_respecte_le_seuil_du_message(self):
        """Le défaut corrigé : sans le seuil, un message FRAIS d'une boîte morte
        partait avec les vieux."""
        self.stop("claude3")
        old = mail.send(self.db, "claude1", "claude3", "Vieux.")
        fresh = mail.send(self.db, "claude1", "claude3", "Frais.")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '8 days'"
                        " WHERE id = %s", (old,))
        report = undeliverable.sweep(self.cfg, self.db)
        [exp] = report["expired"]
        self.assertEqual(exp["messages"], [old])
        self.assertEqual([m["id"] for m in mail.unread(self.db, "claude3")], [fresh])

    def test_epargne_reservation_active_et_boite_relancee(self):
        self.stop("claude3")
        free = mail.send(self.db, "claude1", "claude3", "Libre.")
        reserved = mail.send(self.db, "claude1", "claude3", "En cours de remise.")
        self.db.execute(
            "UPDATE agent_mailbox SET meta = jsonb_build_object('consigne', jsonb_build_object("
            "'jeton', 'j', 'expire', extract(epoch from clock_timestamp()) + 600))"
            " WHERE id = %s", (reserved,))
        self.age(8)
        report = undeliverable.sweep(self.cfg, self.db)
        [exp] = report["expired"]
        self.assertEqual(exp["messages"], [free])
        self.assertEqual([m["id"] for m in mail.unread(self.db, "claude3")], [reserved])
        # un agent VIVANT n'est jamais expiré, même avec du vieux courrier
        self.agent("claude2")
        mail.send(self.db, "claude1", "claude2", "Pour un vivant.")
        self.age(8)
        self.assertEqual(storage.of(self.db).mailbox.expire_dead_letters(
            "claude2", {"by": "test"}, time.time()), [])
        self.assertEqual(len(self.pending("claude2")), 1)

    def test_dry_run_ne_touche_rien(self):
        self.lie()
        self.stop("claude3")
        mail.send(self.db, "claude1", "claude3", "Rapport.")
        self.age(8)
        report = undeliverable.sweep(self.cfg, self.db, dry_run=True)
        [fwd] = report["forwarded"]
        self.assertEqual((fwd["count"], fwd["dry_run"]), (1, True))
        self.assertEqual(len(self.pending("claude3")), 1)          # rien n'a bougé
        # sans successeur : l'expiration à blanc dit ce qui partirait
        self.db.execute("UPDATE session_bindings SET revoked_at = now()")
        report = undeliverable.sweep(self.cfg, self.db, dry_run=True)
        [exp] = report["expired"]
        self.assertEqual((exp["count"], exp["dry_run"]), (1, True))
        self.assertEqual(len(self.pending("claude3")), 1)

    def test_retention_nulle_ou_surchargee(self):
        self.stop("claude3")
        mail.send(self.db, "claude1", "claude3", "Rapport.")
        self.age(30)
        report = undeliverable.sweep(self.cfg, self.db, days=0)
        self.assertEqual((report["days"], report["cutoff"], report["expired"]),
                         (0, None, []))                            # 0 = jamais
        self.assertEqual(len(self.pending("claude3")), 1)
        report = undeliverable.sweep(self.cfg, self.db, days=7)
        self.assertEqual(report["expired"][0]["count"], 1)

    def test_successeur_arrete_refus_puis_retention(self):
        self.lie()
        self.stop("mesh-design")
        self.stop("claude3")
        mail.send(self.db, "claude1", "claude3", "Rapport.")
        self.age(8)
        report = undeliverable.sweep(self.cfg, self.db)
        self.assertEqual(report["forwarded"], [])
        [refused] = report["refused"]
        self.assertEqual((refused["old"], refused["new"]), ("claude3", "mesh-design"))
        self.assertEqual(report["expired"][0]["count"], 1)         # pas d'attente infinie

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_le_menage_de_l_executeur_balaye_et_respecte_dry_run(self):
        import types
        from ameesh import runner as runner_mod
        self.lie()
        self.stop("claude3")
        mail.send(self.db, "claude1", "claude3", "Rapport.")
        fake = types.SimpleNamespace(cfg=self.cfg, dry_run=True, mediated=False, db=None)
        first = runner_mod.Runner.sweep_dead_letters_once(fake, self.db, clock=1000.0)
        self.assertEqual(first["forwarded"][0]["count"], 1)
        self.assertEqual(len(self.pending("claude3")), 1)          # dry-run : rien bougé
        self.assertEqual(fake._dead_letters_next,
                         1000.0 + runner_mod.DEAD_LETTER_SWEEP_EVERY_S)
        self.assertIsNone(runner_mod.Runner.sweep_dead_letters_once(
            fake, self.db, clock=1001.0))                          # au plus une fois par heure
        fake.dry_run = False
        second = runner_mod.Runner.sweep_dead_letters_once(fake, self.db, clock=5000.0)
        self.assertEqual(second["forwarded"][0]["count"], 1)
        self.assertEqual(self.pending("claude3"), [])

    def test_cli_sweep(self):
        self.lie()
        self.stop("claude3")
        mail.send(self.db, "claude1", "claude3", "Rapport.")
        out = self.mesh("mail", "sweep", "--dry-run")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("dry run", out.stdout)
        self.assertIn("claude3 (stopped): 1 message(s) would be re-delivered to mesh-design",
                      out.stdout)
        self.assertEqual(len(self.pending("claude3")), 1)
        out = self.mesh("mail", "sweep", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual(payload["forwarded"][0]["new"], "mesh-design")
        self.assertEqual(self.pending("claude3"), [])
        self.assertEqual(self.mesh("mail", "sweep", "--days", "-1").returncode, 2)

    def test_essaye_les_successeurs_dans_l_ordre(self):
        """#1396/N3 : le premier successeur arrêté ne fait plus expirer le
        courrier quand un suivant est vivant (les deux sont essayés dans
        l'ordre). #1615/N3 : l'ordre est celui des liaisons, la plus récente
        d'abord — le successeur arrêté est donc lié en dernier."""
        self.agent("a-mort")
        self.stop("a-mort")
        registry.set_session(self.db, "claude3", "s-claude3")
        binds = storage.of(self.db).session_bindings
        for harness, agent in (("claude", "mesh-design"), ("codex", "a-mort")):
            binds.bind(host=self.cfg.host, harness=harness, session_id="s-claude3",
                       agent=agent, pid=None, created_by="human:proprio")
        self.stop("claude3")
        mail.send(self.db, "claude1", "claude3", "Rapport 1.")
        self.age(8)                       # au-delà de la rétention : sans le 2e, il expirerait
        report = undeliverable.sweep(self.cfg, self.db, dry_run=True)
        self.assertEqual(report["expired"], [])
        [refused] = report["refused"]
        self.assertEqual((refused["old"], refused["new"]), ("claude3", "a-mort"))
        [fwd] = report["forwarded"]
        self.assertEqual((fwd["old"], fwd["new"], fwd["count"], fwd["dry_run"]),
                         ("claude3", "mesh-design", 1, True))
        self.assertEqual(len(self.pending("claude3")), 1)          # dry-run : rien bougé
        report = undeliverable.sweep(self.cfg, self.db)
        self.assertEqual(report["expired"], [])
        [refused] = report["refused"]
        self.assertEqual((refused["old"], refused["new"]), ("claude3", "a-mort"))
        [fwd] = report["forwarded"]
        self.assertEqual((fwd["old"], fwd["new"], fwd["count"]),
                         ("claude3", "mesh-design", 1))
        self.assertEqual(self.pending("claude3"), [])
        self.assertEqual(self.pending("mesh-design"), ["Rapport 1."])

    def test_cli_sweep_signale_les_refus(self):
        """#1396/N2 : un renvoi refusé reste visible et rend non-zéro, au lieu
        d'être masqué par « no dead letter » (retention 0 = jamais expiré)."""
        self.lie()
        self.stop("mesh-design")
        self.stop("claude3")
        mail.send(self.db, "claude1", "claude3", "Rapport.")
        out = self.mesh("mail", "sweep", "--days", "0")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("re-delivery to mesh-design refused", out.stderr)
        self.assertIn("no dead letter swept", out.stdout)
        self.assertNotIn("no dead letter to sweep", out.stdout)
        self.assertEqual(self.pending("claude3"), ["Rapport."])    # rien n'a bougé
        out = self.mesh("mail", "sweep", "--days", "0", "--json")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual((payload["forwarded"], payload["expired"]), ([], []))
        self.assertEqual(payload["refused"][0]["new"], "mesh-design")

    def test_successeur_rend_la_liste_recente_d_abord(self):
        """#1615/N3 : `successor` rend la liste des repreneurs, liaison la plus
        récente d'abord — pas une chaîne triée alphabétiquement puis
        redécoupée ; un agent lié deux fois ne figure qu'une fois."""
        self.agent("a-mort")
        registry.set_session(self.db, "claude3", "s-claude3")
        binds = storage.of(self.db).session_bindings
        for harness, agent in (("claude", "mesh-design"), ("codex", "a-mort"),
                               ("deepseek", "a-mort")):
            binds.bind(host=self.cfg.host, harness=harness, session_id="s-claude3",
                       agent=agent, pid=None, created_by="human:proprio")
        row = registry.get(self.db, "claude3")
        self.assertEqual(undeliverable.successor(self.db, row),
                         ["a-mort", "mesh-design"])

    def test_le_balayage_quitte_la_boucle_quand_l_ancien_est_relance(self):
        """#1615/N2 : un refus qui porte sur l'ancien (relancé sous le verrou)
        n'est pas réessayé sur chaque successeur : une seule entrée refused, et
        son courrier n'est pas expiré — sa boîte est de nouveau lue."""
        self.agent("a-mort")
        self.stop("a-mort")
        self.stop("claude3")
        registry.set_session(self.db, "claude3", "s-claude3")
        binds = storage.of(self.db).session_bindings
        for harness, agent in (("claude", "mesh-design"), ("codex", "a-mort")):
            binds.bind(host=self.cfg.host, harness=harness, session_id="s-claude3",
                       agent=agent, pid=None, created_by="human:proprio")
        mail.send(self.db, "claude1", "claude3", "Rapport.")
        self.age(8)                        # assez vieux pour expirer sans le renvoi
        essais = []

        def relance_entre_temps(cfg, db, old, new, **kw):
            essais.append(new)
            raise undeliverable.RefusedOld("claude3 is not stopped (status running)")

        vrai_forward = undeliverable.forward
        undeliverable.forward = relance_entre_temps
        try:
            report = undeliverable.sweep(self.cfg, self.db)
        finally:
            undeliverable.forward = vrai_forward
        self.assertEqual(essais, ["a-mort"])       # un seul essai, pas un par successeur
        self.assertEqual([(r["old"], r["new"]) for r in report["refused"]],
                         [("claude3", "a-mort")])
        self.assertEqual(report["expired"], [])
        self.assertEqual(self.pending("claude3"), ["Rapport."])
        # Le courrier sera lu par l'ancien relancé : ce refus n'est pas un
        # échec du balayage (retour 0), il reste dit au rapport.
        from ameesh import cli
        self.assertTrue(report["refused"][0].get("old_relaunched"))
        self.assertEqual(cli._sweep_failed(report), [])

    def test_cli_sweep_ne_rend_pas_1_quand_un_successeur_a_repris(self):
        """#1615/N1 : un refus réparé par un successeur suivant (courrier remis)
        n'est plus un échec — code 0, le refus restant imprimé ; un refus sans
        reprise rend toujours 1 (témoin voisin)."""
        self.agent("a-mort")
        self.stop("a-mort")
        self.stop("claude3")
        registry.set_session(self.db, "claude3", "s-claude3")
        binds = storage.of(self.db).session_bindings
        for harness, agent in (("claude", "mesh-design"), ("codex", "a-mort")):
            binds.bind(host=self.cfg.host, harness=harness, session_id="s-claude3",
                       agent=agent, pid=None, created_by="human:proprio")
        mail.send(self.db, "claude1", "claude3", "Rapport.")
        out = self.mesh("mail", "sweep", "--dry-run")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("re-delivery to a-mort refused", out.stderr)
        self.assertIn("would be re-delivered to mesh-design", out.stdout)
        self.assertEqual(self.pending("claude3"), ["Rapport."])    # dry-run : rien bougé
        out = self.mesh("mail", "sweep", "--dry-run", "--json")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual([(r["old"], r["new"]) for r in payload["refused"]],
                         [("claude3", "a-mort")])
        self.assertEqual(payload["forwarded"][0]["new"], "mesh-design")

    def test_expire_un_nom_absent_du_registre(self):
        """Nom absent : rien à verrouiller, l'absence est constatée dans
        l'instruction d'expiration — pas de ligne de registre créée."""
        mail.send(self.db, "claude1", "fantome", "Rapport.")
        self.age(8)
        report = undeliverable.sweep(self.cfg, self.db)
        [exp] = report["expired"]
        self.assertEqual((exp["recipient"], exp["count"], exp["unknown"]),
                         ("fantome", 1, True))
        self.assertEqual(self.pending("fantome"), [])
        self.assertEqual(self.db.query(
            "SELECT name FROM agent_registry WHERE name = 'fantome'"), [])
        self.assertEqual(self.alertes(), [])

    # -- relecture codex3, B1 : la décision est relue SOUS VERROU -----------
    #
    # Chaque témoin tient une transaction DISTINCTE (une autre connexion) qui
    # n'est pas committée ; le travail du balayage part ensuite et doit
    # ATTENDRE le verrou du registre avant de décider. Sans le verrou, il
    # déciderait sur l'instantané d'avant la relance et expédierait/expirerait
    # le courrier d'un agent relancé.

    class _TemoinDb:
        """ScopedDb d'un fil témoin, qui publie le PID de sa connexion.

        `ScopedDb` ferme sa connexion après chaque opération : le PID n'existe
        que pendant la transaction du fil. Ce proxy le note dès qu'une
        transaction s'ouvre (`out["pid"]`) — c'est cette connexion-là, et pas
        « un verrou quelque part », que les témoins observent (L1056)."""

        def __init__(self, db, out):
            self._db, self._out = db, out

        def __getattr__(self, name):        # query, execute, script, close…
            return getattr(self._db, name)

        @contextmanager
        def transaction(self):
            with self._db.transaction() as tx:
                self._out["pid"] = self._db.query(
                    "SELECT pg_backend_pid()::int AS pid")[0]["pid"]
                yield tx

    def _attend_verrou(self, *needles, out, timeout=10.0):
        """Attend que LA connexion du fil témoin (`out["pid"]`) soit réellement
        bloquée sur un verrou (le texte de sa requête porte l'un des
        `needles`).

        L1056 (N1 de #976) : `pg_stat_activity` est celui de tout le cluster —
        une suite parallèle qui exécute la même requête donnait le signal à
        notre place, et `is_alive` ne prouvait pas l'attente. Le PID de la
        connexion témoin est donc exigé : on n'observe qu'elle."""
        clause = " OR ".join(["query LIKE %s"] * len(needles))
        fin = time.monotonic() + timeout
        pid = None
        while time.monotonic() < fin:
            pid = out.get("pid")            # posé à l'ouverture de la transaction
            if pid:
                rows = self.db.query(
                    "SELECT count(*)::int AS n FROM pg_stat_activity"
                    " WHERE wait_event_type = 'Lock' AND pid = %s AND (%s)"
                    % (pid, clause), tuple("%" + needle + "%" for needle in needles))
                if rows[0]["n"]:
                    return
            time.sleep(0.05)
        self.fail("aucune attente de verrou Postgres de la connexion témoin"
                  " (pid %s) sur %s : %r" % (pid, (needles,), out.get("error")))

    def _travail_concurrent(self, action):
        """Lance `action(connexion)` dans un fil, rend (fil, resultats).

        `resultats["pid"]` reçoit le PID Postgres de la connexion témoin dès
        que l'action ouvre sa transaction (L1056)."""
        out: dict = {}

        def run():
            try:
                out["value"] = action(self._TemoinDb(ScopedDb(self.cfg), out))
            except BaseException as exc:       # repris par le test
                out["error"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        return thread, out

    def _inscrire(self, db, name, *, status=None):
        """Inscrit `name` DANS une transaction tenue sur une seule connexion :
        c'est elle que le témoin observe, et le verrou par nom reste tenu
        jusqu'au commit (L1056)."""
        with db.transaction() as tx:
            return registry.upsert(tx, name, status=status)

    def test_expire_attend_une_relance_concurrente(self):
        self.stop("claude3")
        mid = mail.send(self.db, "claude1", "claude3", "Rapport.")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '8 days'"
                        " WHERE id = %s", (mid,))
        started, release = threading.Event(), threading.Event()
        errors = []

        def hold():
            try:
                with ScopedDb(self.cfg).transaction() as tx:
                    tx.execute("UPDATE agent_registry SET status = 'running'"
                               " WHERE name = 'claude3'")
                    tx.execute("SELECT id FROM agent_mailbox WHERE id = %s FOR UPDATE", (mid,))
                    started.set()
                    release.wait(15)
            except BaseException as exc:
                errors.append(exc)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(started.wait(15), "la transaction concurrente n'a pas démarré")
        worker, out = self._travail_concurrent(
            lambda db: storage.of(db).mailbox.expire_dead_letters(
                "claude3", {"by": "test"}, time.time()))
        self._attend_verrou("jsonb_build_object('expired'", out=out)
        release.set()
        holder.join(20)
        worker.join(20)
        self.assertFalse(holder.is_alive() or worker.is_alive())
        self.assertEqual(errors, [])
        self.assertNotIn("error", out, out.get("error"))
        self.assertEqual(out.get("value"), [])              # la relance gagne : rien expiré
        self.assertEqual(self.pending("claude3"), ["Rapport."])

    def test_forward_refuse_une_relance_concurrente_de_old(self):
        self.stop("claude3")
        mid = mail.send(self.db, "claude1", "claude3", "Rapport.")
        started, release = threading.Event(), threading.Event()
        errors = []

        def hold():
            try:
                with ScopedDb(self.cfg).transaction() as tx:
                    tx.execute("UPDATE agent_registry SET status = 'running'"
                               " WHERE name = 'claude3'")
                    tx.execute("SELECT id FROM agent_mailbox WHERE id = %s FOR UPDATE", (mid,))
                    started.set()
                    release.wait(15)
            except BaseException as exc:
                errors.append(exc)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(started.wait(15), "la transaction concurrente n'a pas démarré")
        worker, out = self._travail_concurrent(
            lambda db: storage.of(db).mailbox.forward("claude3", "mesh-design", "test"))
        self._attend_verrou("ORDER BY name FOR UPDATE", "ORDER BY m.id FOR UPDATE",
                            out=out)
        release.set()
        holder.join(20)
        worker.join(20)
        self.assertFalse(holder.is_alive() or worker.is_alive())
        self.assertEqual(errors, [])
        self.assertIsInstance(out.get("error"), storage.MailboxRefused, out)
        self.assertEqual(self.pending("claude3"), ["Rapport."])
        self.assertEqual(self.pending("mesh-design"), [])

    def test_forward_distingue_un_refus_du_stockage_venu_de_l_ancien(self):
        """#1615/N2 : quand le stockage refuse sous verrou parce que l'ancien a
        été relancé, `undeliverable.forward` le classe `RefusedOld` (et non un
        refus du successeur) : le balayage ne réessaiera pas les suivants."""
        self.stop("claude3")
        mid = mail.send(self.db, "claude1", "claude3", "Rapport.")
        started, release = threading.Event(), threading.Event()
        errors = []

        def hold():
            try:
                with ScopedDb(self.cfg).transaction() as tx:
                    tx.execute("UPDATE agent_registry SET status = 'running'"
                               " WHERE name = 'claude3'")
                    tx.execute("SELECT id FROM agent_mailbox WHERE id = %s FOR UPDATE", (mid,))
                    started.set()
                    release.wait(15)
            except BaseException as exc:
                errors.append(exc)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(started.wait(15), "la transaction concurrente n'a pas démarré")
        worker, out = self._travail_concurrent(
            lambda db: undeliverable.forward(self.cfg, db, "claude3", "mesh-design",
                                             actor="test"))
        self._attend_verrou("ORDER BY name FOR UPDATE", "ORDER BY m.id FOR UPDATE",
                            out=out)
        release.set()
        holder.join(20)
        worker.join(20)
        self.assertFalse(holder.is_alive() or worker.is_alive())
        self.assertEqual(errors, [])
        self.assertIsInstance(out.get("error"), undeliverable.RefusedOld, out)
        self.assertEqual(self.pending("claude3"), ["Rapport."])
        self.assertEqual(self.pending("mesh-design"), [])

    def test_forward_refuse_un_arret_concurrent_de_new(self):
        self.stop("claude3")
        mail.send(self.db, "claude1", "claude3", "Rapport.")
        started, release = threading.Event(), threading.Event()
        errors = []

        def hold():
            try:
                with ScopedDb(self.cfg).transaction() as tx:
                    tx.execute("UPDATE agent_registry SET status = 'stopped'"
                               " WHERE name = 'mesh-design'")
                    started.set()
                    release.wait(15)
            except BaseException as exc:
                errors.append(exc)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(started.wait(15), "la transaction concurrente n'a pas démarré")
        worker, out = self._travail_concurrent(
            lambda db: storage.of(db).mailbox.forward("claude3", "mesh-design", "test"))
        self._attend_verrou("ORDER BY name FOR UPDATE", out=out)
        release.set()
        holder.join(20)
        worker.join(20)
        self.assertFalse(holder.is_alive() or worker.is_alive())
        self.assertEqual(errors, [])
        self.assertIsInstance(out.get("error"), storage.MailboxRefused, out)
        self.assertEqual(self.pending("claude3"), ["Rapport."])
        self.assertEqual(self.pending("mesh-design"), [])

    # -- relecture codex3, B1 RÉSIDUEL : l'absence se sérialise aussi --------
    #
    # `FOR SHARE`/`FOR UPDATE` ne verrouillent que les lignes PRÉSENTES : une
    # inscription concurrente d'un nom absent pouvait se committer entre la
    # lecture d'absence et l'écriture. Les chemins de création prennent
    # désormais le même verrou advisory par nom (`registry.NAME_LOCK_*`) que la
    # décision ; ces témoins exigent que l'inscription ATTENDE ce verrou.
    #
    # L1056 (N1 de #976) : chaque attente est observée sur la connexion du fil
    # concerné (`out["pid"]`) — un `pg_advisory_xact_lock` vu dans une autre
    # suite parallèle ne donne plus le signal — et le sens inverse est couvert :
    # l'inscription gagne le verrou, la décision attend puis voit l'agent
    # vivant (rien expiré, renvoi refusé).

    def _attend_verrou_inscription(self, register, out):
        """L'inscription de `register` attend le verrou par nom, sur SA
        connexion (`out["pid"]`), et pas seulement « un verrou quelque part »."""
        fin = time.monotonic() + 10.0
        pid, seen = None, None
        while time.monotonic() < fin:
            pid = out.get("pid")           # posé à l'ouverture de la transaction
            if pid:
                rows = self.db.query(
                    "SELECT wait_event_type, wait_event, query FROM pg_stat_activity"
                    " WHERE pid = %s", (pid,))
                seen = rows[0] if rows else None
                if (seen and seen["wait_event_type"] == "Lock"
                        and seen["wait_event"] == "advisory"
                        and "pg_advisory_xact_lock" in (seen["query"] or "")):
                    self.assertTrue(register.is_alive(),
                                    "l'inscription a devancé la décision")
                    return
            time.sleep(0.05)
        self.fail("l'inscription (pid %s) n'attend pas le verrou par nom : %r"
                  % (pid, seen or out.get("error")))

    def _inscription_gagnante(self, name, status, started, release):
        """Ouvre une transaction qui INSCRIT `name` — le verrou par nom est donc
        tenu — pose `started`, puis attend `release` avant de committer.
        Rend (fil, erreurs)."""
        errors = []

        def hold():
            try:
                with ScopedDb(self.cfg).transaction() as tx:
                    registry.upsert(tx, name, status=status)
                    started.set()
                    release.wait(15)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=hold)
        thread.start()
        return thread, errors

    def test_expire_nom_absent_attend_une_inscription_concurrente(self):
        mid = mail.send(self.db, "claude1", "fantome", "Rapport.")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '8 days'"
                        " WHERE id = %s", (mid,))
        started, release = threading.Event(), threading.Event()
        errors = []

        def hold():
            try:
                with ScopedDb(self.cfg).transaction() as tx:
                    tx.execute("SELECT id FROM agent_mailbox WHERE id = %s FOR UPDATE", (mid,))
                    started.set()
                    release.wait(15)
            except BaseException as exc:
                errors.append(exc)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(started.wait(15), "la transaction concurrente n'a pas démarré")
        worker, out = self._travail_concurrent(
            lambda db: storage.of(db).mailbox.expire_dead_letters(
                "fantome", {"by": "test"}, time.time()))
        self._attend_verrou("jsonb_build_object('expired'", out=out)
        register, reg_out = self._travail_concurrent(
            lambda db: self._inscrire(db, "fantome", status="idle"))
        self._attend_verrou_inscription(register, reg_out)
        release.set()
        holder.join(20)
        worker.join(20)
        register.join(20)
        self.assertFalse(holder.is_alive() or worker.is_alive() or register.is_alive())
        self.assertEqual(errors, [])
        self.assertNotIn("error", out, out.get("error"))
        self.assertNotIn("error", reg_out, reg_out.get("error"))
        # ordre sérialisé : l'expiration (nom absent à sa décision) commite
        # avant l'inscription, qui attendait le verrou par nom
        self.assertEqual([r["id"] for r in out["value"]], [mid])
        self.assertEqual(reg_out["value"]["name"], "fantome")
        self.assertEqual(self.db.query(
            "SELECT status FROM agent_registry WHERE name = 'fantome'"),
            [{"status": "idle"}])
        self.assertEqual(self.pending("fantome"), [])

    def test_forward_nom_absent_attend_une_inscription_concurrente(self):
        mid = mail.send(self.db, "claude1", "fantome", "Rapport.")
        started, release = threading.Event(), threading.Event()
        errors = []

        def hold():
            try:
                with ScopedDb(self.cfg).transaction() as tx:
                    tx.execute("SELECT id FROM agent_mailbox WHERE id = %s FOR UPDATE", (mid,))
                    started.set()
                    release.wait(15)
            except BaseException as exc:
                errors.append(exc)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(started.wait(15), "la transaction concurrente n'a pas démarré")
        worker, out = self._travail_concurrent(
            lambda db: storage.of(db).mailbox.forward("fantome", "mesh-design", "test"))
        self._attend_verrou("ORDER BY m.id FOR UPDATE", out=out)
        register, reg_out = self._travail_concurrent(
            lambda db: self._inscrire(db, "fantome", status="idle"))
        self._attend_verrou_inscription(register, reg_out)
        release.set()
        holder.join(20)
        worker.join(20)
        register.join(20)
        self.assertFalse(holder.is_alive() or worker.is_alive() or register.is_alive())
        self.assertEqual(errors, [])
        self.assertNotIn("error", out, out.get("error"))
        self.assertNotIn("error", reg_out, reg_out.get("error"))
        # le renvoi (old absent à sa décision) commite avant l'inscription
        self.assertEqual(len(out["value"]), 1)
        self.assertEqual(self.pending("fantome"), [])
        self.assertEqual(self.pending("mesh-design"), ["Rapport."])
        self.assertEqual(reg_out["value"]["name"], "fantome")

    # -- L1056 : le sens inverse — l'inscription gagne, la décision attend ----

    def test_expire_si_l_inscription_gagne_le_verrou(self):
        """L'inscription gagne le verrou par nom ; l'expiration attend SUR SA
        connexion, puis la voit et n'expire pas le courrier d'un vivant."""
        mid = mail.send(self.db, "claude1", "fantome", "Rapport.")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '8 days'"
                        " WHERE id = %s", (mid,))
        started, release = threading.Event(), threading.Event()
        holder, held = self._inscription_gagnante("fantome", "running", started, release)
        self.assertTrue(started.wait(15), "l'inscription gagnante n'a pas pris le verrou")
        worker, out = self._travail_concurrent(
            lambda db: storage.of(db).mailbox.expire_dead_letters(
                "fantome", {"by": "test"}, time.time()))
        self._attend_verrou("pg_advisory_xact_lock", out=out)
        self.assertTrue(worker.is_alive(), "l'expiration a devancé l'inscription")
        release.set()
        holder.join(20)
        worker.join(20)
        self.assertFalse(holder.is_alive() or worker.is_alive())
        self.assertEqual(held, [])
        self.assertNotIn("error", out, out.get("error"))
        self.assertEqual(out.get("value"), [])       # l'agent est vivant : rien expiré
        self.assertEqual(self.pending("fantome"), ["Rapport."])
        self.assertEqual(self.db.query(
            "SELECT status FROM agent_registry WHERE name = 'fantome'"),
            [{"status": "running"}])

    def test_forward_si_l_inscription_gagne_le_verrou(self):
        """L'inscription de `old` gagne le verrou par nom ; le renvoi attend SUR
        SA connexion, puis le voit vivant et refuse de renvoyer son courrier."""
        mid = mail.send(self.db, "claude1", "fantome", "Rapport.")
        started, release = threading.Event(), threading.Event()
        holder, held = self._inscription_gagnante("fantome", "running", started, release)
        self.assertTrue(started.wait(15), "l'inscription gagnante n'a pas pris le verrou")
        worker, out = self._travail_concurrent(
            lambda db: storage.of(db).mailbox.forward("fantome", "mesh-design", "test"))
        self._attend_verrou("pg_advisory_xact_lock", out=out)
        self.assertTrue(worker.is_alive(), "le renvoi a devancé l'inscription")
        release.set()
        holder.join(20)
        worker.join(20)
        self.assertFalse(holder.is_alive() or worker.is_alive())
        self.assertEqual(held, [])
        self.assertIsInstance(out.get("error"), storage.MailboxRefused, out)
        self.assertEqual(self.pending("fantome"), ["Rapport."])
        self.assertEqual(self.pending("mesh-design"), [])
        self.assertEqual(self.db.query(
            "SELECT status FROM agent_registry WHERE name = 'fantome'"),
            [{"status": "running"}])


if __name__ == "__main__":
    unittest.main()
