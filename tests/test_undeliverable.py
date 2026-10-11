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

import json
import os
import time
import unittest

from ameesh import exploitation, mail, notify, projects, registry, storage, undeliverable
from ameesh.backend import PgBackend

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

    def test_noms_proches(self):
        rows = [{"name": n, "team": t, "status": s} for n, t, s in (
            ("orchestrateur", "manaty", "idle"), ("claude1", "manaty", "idle"),
            ("claude2", "manaty", "running"), ("claude3", "manaty", "stopped"),
            ("deepseek1", "manaty", "idle"), ("dev", "", "idle"))]
        near = undeliverable.suggestions("orchestrator", rows, role_names=["orchestrateur"],
                                         team="manaty")
        self.assertEqual(near, [("orchestrateur", "orchestrateur de l'équipe manaty")])
        near = undeliverable.suggestions("claude33", rows)
        self.assertEqual(near[0], ("claude3", "1 lettre d'écart, arrêté"))  # le plus proche
        self.assertEqual(near[1], ("claude1", "2 lettres d'écart"))
        near = [n for n, _ in undeliverable.suggestions("deep", rows)]
        self.assertEqual(near, ["deepseek1"])                  # même début
        self.assertEqual(undeliverable.suggestions("mesh", [{"name": "mesh-design"}]),
                         [("mesh-design", "même début")])
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
        self.assertIn("15 message(s) adressé(s) à claude3", briefs["orchestrateur"])
        self.assertIn("ameesh mail forward claude3 mesh-design", briefs["orchestrateur"])
        alerte.update(agent="orchestrator", reason="inconnu", suggestions=["orchestrateur"])
        text = undeliverable.orchestrator_briefs(alerte)["orchestrateur"]
        self.assertIn("« orchestrator », absent du registre", text)
        self.assertIn("Noms proches : orchestrateur", text)
        self.assertEqual(undeliverable.orchestrator_briefs({"type": "idle_capacity"}), {})

    def test_consigne_et_hook_disent_le_renvoi(self):
        row = {"id": 7, "sender": "claude1", "created_ts": NOW, "body": "SHA abc123 fusionné",
               "kind": "notify", "payload": {}, "forwarded_from": "claude3"}
        texte, _retenus, _reste = mail.prompt_for(None, [row], kind="mail")
        self.assertIn("[renvoyé : d'abord adressé à claude3", texte)
        from ameesh import cli
        rendu = cli.render([mail.normalize(row)])
        self.assertIn("[renvoyé : d'abord adressé à claude3", rendu)
        # un message ordinaire : rendu inchangé (JSON des hooks identique à la v0)
        self.assertNotIn("renvoyé", cli.render([mail.normalize(dict(row, forwarded_from=None))]))

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
        self.assertIn("42 en souffrance", text)
        self.assertIn("! 42 message(s) en souffrance : claude3 (arrêté) 15, orchestrator "
                      "(inconnu) 27", text)
        # sans courrier en souffrance : rien ne change
        view = projects.build(dict(board, dead_letters=[]), now=NOW, paid_harnesses=())
        self.assertEqual(view["undeliverable"], 0)
        self.assertNotIn("souffrance", projects.format_text(view, 120))


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
        self.assertIn("message non déposé — « orchestrator » n'est pas dans le registre",
                      proc.stderr)
        # le rôle : l'orchestrateur de l'équipe de l'expéditeur, pas celui d'une autre
        self.assertIn("noms proches : orchestrateur (orchestrateur de l'équipe manaty)",
                      proc.stderr)
        self.assertNotIn("chef-b", proc.stderr)
        self.assertIsNone(registry.get(self.db, "orchestrator"))   # aucun fantôme
        self.assertEqual(self.pending("orchestrator"), [])
        # distance d'édition, sans rôle déclaré
        proc = self.send("claude33", "Bonjour.", AGENT_MAIL_NAME="claude1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("claude3 (1 lettre d'écart)", proc.stderr)
        self.assertIsNone(registry.get(self.db, "claude33"))
        # « all » reste une destination ; un nom illisible reste invalide
        proc = self.send("all", "Point d'équipe.", AGENT_MAIL_NAME="claude1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.send("human:proprio", "x", AGENT_MAIL_NAME="claude1").returncode,
                         2)

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
        self.assertIn("message non déposé — claude3 est arrêté (depuis le ", proc.stderr)
        self.assertIn("raison : manuel — arrêté à la main", proc.stderr)
        self.assertIn("responsable : human:proprio", proc.stderr)
        self.assertIn("son travail a été repris par mesh-design", proc.stderr)
        self.assertIn("--queue", proc.stderr)
        self.assertEqual(self.pending("claude3"), [])
        proc = self.send("claude3", "Pour ta reprise.", "--queue", AGENT_MAIL_NAME="claude1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("déposé pour : claude3", proc.stdout)
        self.assertIn("attention : claude3 est arrêté", proc.stderr)
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
        self.assertIn("l'expéditeur claude3 est arrêté", proc.stderr)
        self.assertIn("identité liée à cette session (agent-mail whoami) : mesh-design",
                      proc.stderr)
        self.assertIn("renvoyez sans --from", proc.stderr)
        # identité résolue de la session (AGENT_MAIL_NAME) : refusée aussi
        proc = self.send("orchestrateur", "Rapport.", AGENT_MAIL_NAME="claude3")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cette session parle au nom de claude3", proc.stderr)
        # identité liée au bail d'un agent arrêté : la raison est dite
        proc = self.send("orchestrateur", "Rapport.", AGENT_MAIL_NAME="claude3",
                         AMEESH_RUNNER_ID="runner-x", AMEESH_LEASE_EPOCH="1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("expéditeur non lié (claude3 : agent arrêté)", proc.stderr)
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
        self.assertIn("cette session est liée à claude3", proc.stderr)
        self.assertIn("agent-mail unbind --session s-1 --harness claude", proc.stderr)

    def test_diffusion_ecarte_les_agents_arretes(self):
        self.stop("claude3")
        proc = self.send("all", "Point d'équipe à 15 h.", AGENT_MAIL_NAME="orchestrateur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("claude3", proc.stdout)
        self.assertIn("non déposé pour claude3 (arrêté", proc.stderr)
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
        self.assertIn("Courrier en souffrance : 1 message(s) adressé(s) à claude3",
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
        self.assertIn("2 message(s) en souffrance", out.stdout)


class RenvoiTest(_Base):
    """`ameesh mail forward <ancien> <nouveau>` : vider une boîte morte."""

    def setUp(self) -> None:
        super().setUp()
        for name in ("claude1", "mesh-design", "orchestrateur"):
            self.agent(name)
        self.agent("claude3")

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
        self.assertIn("à blanc : 2 message(s) en attente pour claude3 (arrêté)", proc.stdout)
        self.assertEqual(len(mail.unread(self.db, "claude3")), 2)
        proc = self.mesh("mail", "forward", "claude3", "mesh-design",
                         env=self.env(AGENT_MAIL_NAME="orchestrateur"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2 message(s) re-livré(s) de claude3 (arrêté) à mesh-design", proc.stdout)
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
        self.assertIn("— de claude1 à ", contexte)
        self.assertIn("[renvoyé : d'abord adressé à claude3", contexte)
        fil = self.mesh("fil", "show", "manaty")
        self.assertIn("Renvoi de 2 message(s) en souffrance : la boîte de claude3", fil.stdout)
        # plus rien à renvoyer
        proc = self.mesh("mail", "forward", "claude3", "mesh-design")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("rien à renvoyer", proc.stdout)

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
        self.assertIn("renvoi refusé — claude3 n'est pas arrêté", proc.stderr)
        self.assertIn("agent-runner stop claude3", proc.stderr)
        self.assertEqual(self.pending("claude3"), ["Pour toi."])
        self.stop("claude3")
        # vers un nom inconnu ou un agent arrêté : refusé
        with self.assertRaises(undeliverable.Refused) as ctx:
            undeliverable.forward(self.cfg, self.db, "claude3", "mesh-desing", actor="x")
        self.assertIn("mesh-design (1 lettre d'écart)", str(ctx.exception))
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


if __name__ == "__main__":
    unittest.main()
