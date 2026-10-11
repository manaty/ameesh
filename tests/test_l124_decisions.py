# SPDX-License-Identifier: AGPL-3.0-only
"""L124 : demandes de décision au propriétaire, reçues et répondues au même
endroit ; L123 : `ameesh chat`, l'agent de conversation du propriétaire.

Les chemins « réponse d'un humain » de bout en bout (sous-processus) passent
le contrôle d'ascendance : lancés depuis la session d'un agent (un agent qui
développe ameesh lance ces tests), ils seraient refusés — à raison — et sont
alors sautés ; la CI les passe. Les mêmes règles sont testées en processus,
avec une ascendance injectée.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

from ameesh import (authority, chat, decisions, exploitation, notify, platform, projects,
                    registry, signing, sous_utilisation, storage, work)
from ameesh import session_bindings as sb

from .support import FAKEBIN, PgTestCase

ALICE = "human:alice"
BOB = "human:bob"
#: variables d'identité retirées de l'environnement des tests en processus
_IDENTITY = ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
             "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH", "AMEESH_CHAT", "AMEESH_CHAT_PID")


def _clean_chain():
    """Ascendance et environnements injectés : aucun agent au-dessus."""
    return dict(chain=[os.getpid()], environ_of=lambda pid: {})


def _humain_hors_agent() -> bool:
    """Ce processus de test ne descend-il d'aucune session d'agent ?"""
    return decisions.agent_in_ancestry() is None


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k not in _IDENTITY}
        env["USER"] = "alice"
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("orch", "dev1"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET responsible = %s WHERE name = 'orch'",
                        (ALICE,))
        self.lot = work.add(self.db, title="RT-9 : cercle de confiance", assignee="dev1")
        work.move(self.db, self.lot["id"], "build")

    def ask(self, **kw):
        kw.setdefault("requester", "orch")
        kw.setdefault("question", "Fusionner la branche avant la démo ?")
        kw.setdefault("options", ["a=oui, fusionner maintenant", "b=non, attendre la revue"])
        kw.setdefault("lot", str(self.lot["id"]))
        return decisions.ask(self.cfg, self.db, **kw)

    def answer(self, decision_id, text, human=ALICE, channel="cli", **kw):
        return decisions.answer(self.cfg, self.db, decision_id, text, human=human,
                                channel=channel, **kw)

    def lot_state(self, lot=None):
        return work.get(self.db, (lot or self.lot)["id"])["state"]

    def mesh_env(self, **extra):
        extra.setdefault("USER", "alice")
        return self.env(**extra)


# ==========================================================================
# demande
# ==========================================================================

class DemandeTest(_Base):
    def test_demande_rattachee_au_lot_et_au_responsable(self):
        view = self.ask(recommend="a", why="la démo en dépend", urgent=True)
        self.assertEqual((view["id"], view["state"], view["owner"], view["owner_source"],
                          view["requester"], view["lot"], view["urgent"], view["recommend"]),
                         (1, "pending", ALICE, "agent", "orch", self.lot["id"], True, "a"))
        self.assertEqual(view["lot_state"], "waiting_human")
        self.assertEqual(view["previous_state"], "build")
        self.assertAlmostEqual(view["due_ts"] - view["created_ts"], decisions.DEFAULT_DUE_S,
                               delta=5)
        row = self.db.query("SELECT kind, recipient, work_item_id, status, delivered_at "
                            "FROM agent_mailbox")[0]
        self.assertEqual((row["kind"], row["recipient"], row["work_item_id"], row["status"],
                          row["delivered_at"]),
                         ("request", ALICE, str(self.lot["id"]), "pending", None))
        self.assertEqual(self.lot_state(), "waiting_human")
        notes = [e["note"] for e in work.events(self.db, self.lot["id"])]
        self.assertTrue(any(n.startswith("décision #1 demandée à human:alice : Fusionner")
                            for n in notes), notes)
        # au fil du projet, sur le lot : la question, les options, comment répondre
        texte = ""
        for root, _dirs, files in os.walk(self.cfg.threads_root):
            for name in files:
                with open(os.path.join(root, name), encoding="utf-8") as fh:
                    texte += fh.read()
        for attendu in ("Décision #1 demandée à human:alice par orch", "a) oui, fusionner",
                        "← recommandé", "Répondre : ameesh decide 1"):
            self.assertIn(attendu, texte)

    def test_destinataire_dans_l_ordre(self):
        # 1. le responsable de la fiche du plan du lot, en remontant au parent
        packages = storage.of(self.db).packages
        packages.upsert({"id": "E1", "kind": "epic", "title": "epic",
                         "responsible": BOB, "canon_ref": "plan/e1.md", "team": "nexus"})
        packages.upsert({"id": "L1", "kind": "lot", "title": "lot", "parent": "E1",
                         "canon_ref": "plan/l1.md"})
        self.db.execute("UPDATE work_items SET package_id = 'L1' WHERE id = %s",
                        (self.lot["id"],))
        view = self.ask()
        self.assertEqual((view["owner"], view["owner_source"]), (BOB, "lot"))
        # 2. le responsable du demandeur
        autre = work.add(self.db, title="autre lot", assignee="dev1")
        self.assertEqual(self.ask(lot=str(autre["id"]))["owner_source"], "agent")
        # 3. le responsable de l'assigné, quand le demandeur n'en a pas
        self.db.execute("UPDATE agent_registry SET responsible = NULL WHERE name = 'orch'")
        self.db.execute("UPDATE agent_registry SET responsible = %s WHERE name = 'dev1'",
                        ("human:carol",))
        troisieme = work.add(self.db, title="troisième", assignee="dev1")
        self.assertEqual(self.ask(lot=str(troisieme["id"]))["owner"], "human:carol")
        # 4. personne : tout humain du mesh
        self.db.execute("UPDATE agent_registry SET responsible = NULL")
        quatrieme = work.add(self.db, title="quatrième")
        view = self.ask(lot=str(quatrieme["id"]))
        self.assertIsNone(view["owner"])
        self.assertEqual(self.db.query("SELECT recipient FROM agent_mailbox WHERE id = %s",
                                       (view["id"],))[0]["recipient"], decisions.OWNER_ANY)

    def test_humain_par_defaut_ou_seul_humain(self):
        self.db.execute("UPDATE agent_registry SET responsible = NULL")
        import dataclasses
        cfg = dataclasses.replace(self.cfg, notify={"default_human": "human:dora"})
        view = decisions.ask(cfg, self.db, requester="orch", question="Quel nom ?",
                             lot=str(self.lot["id"]))
        self.assertEqual((view["owner"], view["owner_source"]), ("human:dora", "defaut"))
        cfg = dataclasses.replace(self.cfg, humans="erin")
        lot = work.add(self.db, title="encore")
        view = decisions.ask(cfg, self.db, requester="orch", question="Quel nom ?",
                             lot=str(lot["id"]))
        self.assertEqual((view["owner"], view["owner_source"]), ("human:erin", "humain_unique"))

    def test_nouveau_lot(self):
        view = self.ask(lot=None, new_lot="DEC-1 : choisir l'hébergeur")
        item = work.get(self.db, view["lot"])
        self.assertEqual((item["title"], item["assignee"], item["state"]),
                         ("DEC-1 : choisir l'hébergeur", "orch", "waiting_human"))
        self.assertEqual(view["previous_state"], "intake")

    def test_refus(self):
        cas = [
            (dict(lot=None), "exactement un"),
            (dict(new_lot="x"), "exactement un"),
            (dict(lot="9999"), "introuvable"),
            (dict(options=["a=un", "a=deux"]), "en double"),
            (dict(options=["a b=un"]), "illisible"),
            (dict(recommend="c"), "pas une des options"),
            (dict(options=[], recommend="a"), "sans option"),
            (dict(why="parce que"), "accompagne --recommend"),
            (dict(question="  "), "question vide"),
            (dict(due_ts=time.time() - 10), "déjà passée"),
        ]
        for kw, attendu in cas:
            with self.subTest(kw=kw), self.assertRaises(decisions.DecisionError) as ctx:
                self.ask(**kw)
            self.assertIn(attendu, str(ctx.exception))
        work.close(self.db, self.lot["id"], abandoned=True)
        with self.assertRaises(decisions.DecisionError) as ctx:
            self.ask()
        self.assertIn("déjà closed", str(ctx.exception))
        self.assertEqual(self.db.query("SELECT count(*)::int AS n FROM agent_mailbox")[0]["n"], 0)

    def test_echeances(self):
        now = time.mktime((2026, 10, 11, 10, 0, 0, 0, 0, -1))
        self.assertEqual(decisions.parse_due("2h", now), now + 7200)
        self.assertEqual(decisions.parse_due("18:30", now), now + 8.5 * 3600)
        self.assertEqual(decisions.parse_due("09:00", now), now + 23 * 3600)
        self.assertEqual(decisions.parse_due("2026-10-12 09:00", now), now + 23 * 3600)
        with self.assertRaises(decisions.DecisionError):
            decisions.parse_due("bientôt", now)

    def test_cli_demande_puis_file(self):
        proc = self.mesh("decide", "ask", "--lot", "RT-9", "--question",
                         "Fusionner la branche avant la démo ?", "--option", "a=oui",
                         "--option", "b=non", "--recommend", "a", "--why", "la démo en dépend",
                         "--urgent", "--by", "2h", "--json",
                         env=self.mesh_env(AGENT_MAIL_NAME="orch"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        view = json.loads(proc.stdout)
        self.assertEqual((view["schema"], view["owner"], view["lot"], view["urgent"]),
                         ("ameesh-decision/1", ALICE, self.lot["id"], True))
        self.ask(question="Deuxième question ?")
        proc = self.mesh("decisions", "--json", env=self.mesh_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        queue = json.loads(proc.stdout)
        self.assertEqual([r["id"] for r in queue["requests"]], [1, 2])   # la plus ancienne d'abord
        self.assertEqual(queue["requests"][0]["options"],
                         [{"key": "a", "label": "oui"}, {"key": "b", "label": "non"}])
        texte = self.mesh("decisions", env=self.mesh_env()).stdout
        for attendu in ("demandes des agents (la plus ancienne d'abord)",
                        "Fusionner la branche avant la démo ?", "a) oui   ← recommandé",
                        "recommandation : a — la démo en dépend", "[URGENT]",
                        "répondre : ameesh decide 1"):
            self.assertIn(attendu, texte)
        # le lot tenu par la demande n'est pas répété parmi les lots en attente
        self.assertNotIn("lots en attente d'un humain", texte)
        # sans identité d'agent : refus, rien n'est déposé
        proc = self.mesh("decide", "ask", "--lot", "RT-9", "--question", "x ?",
                         env=self.mesh_env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("demandeur non lié", proc.stderr)
        self.assertEqual(len(decisions.pending(self.db)), 2)


# ==========================================================================
# réponse
# ==========================================================================

class ReponseTest(_Base):
    def test_option_retour_du_lot_courrier_et_fil(self):
        view = self.ask()
        out = self.answer(view["id"], "a")
        decision = out["decision"]
        self.assertEqual(decision["state"], "answered")
        self.assertEqual({k: decision["answer"][k] for k in ("by", "channel", "text", "option",
                                                               "option_label")},
                         {"by": ALICE, "channel": "cli", "text": "a", "option": "a",
                          "option_label": "oui, fusionner maintenant"})
        self.assertEqual(out["lot"], {"lot": self.lot["id"], "state": "build", "moved": True,
                                      "others": 0})
        self.assertEqual(self.lot_state(), "build")
        # la réponse part au demandeur (elle le réveille), liée au lot
        rows = self.db.query("SELECT sender, kind, body, payload, work_item_id, delivered_at "
                             "FROM agent_mailbox WHERE recipient = 'orch'")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["sender"], rows[0]["kind"], rows[0]["work_item_id"],
                          rows[0]["delivered_at"]), (ALICE, "reply", str(self.lot["id"]), None))
        self.assertEqual(rows[0]["payload"]["decision_answer"]["option"], "a")
        self.assertIn("Décision #1 — réponse du propriétaire human:alice (cli)", rows[0]["body"])
        self.assertIn("Texte exact : « a »", rows[0]["body"])
        self.assertEqual(decisions.pending(self.db), [])
        notes = [e["note"] for e in work.events(self.db, self.lot["id"])]
        self.assertTrue(any("décision #1 répondue par human:alice (cli) : option a" in n
                            and "le lot revient en build" in n for n in notes), notes)
        with self.assertRaises(decisions.DecisionError) as ctx:
            self.answer(view["id"], "b")
        self.assertIn("déjà répondue par human:alice", str(ctx.exception))

    def test_texte_libre_garde_tel_quel(self):
        view = self.ask()
        texte = "b, mais pas avant lundi : la revue d'abord"
        decision = self.answer(view["id"], texte)["decision"]
        self.assertEqual((decision["answer"]["option"], decision["answer"]["text"]),
                         (None, texte))
        self.assertEqual(decisions.match_option(view, " A "), "a")

    def test_seul_l_humain_vise(self):
        view = self.ask()
        with self.assertRaises(decisions.DecisionError) as ctx:
            self.answer(view["id"], "a", human=BOB)
        self.assertIn("adressée à human:alice", str(ctx.exception))
        # sans destinataire précis : tout humain répond
        self.db.execute("UPDATE agent_registry SET responsible = NULL")
        autre = work.add(self.db, title="sans responsable")
        libre = self.ask(lot=str(autre["id"]))
        self.assertEqual(self.answer(libre["id"], "b", human=BOB)["decision"]["answer"]["by"],
                         BOB)

    def test_plusieurs_demandes_sur_un_lot(self):
        first = self.ask()
        second = self.ask(question="Et la date de livraison ?", options=[])
        self.assertEqual(second["previous_state"], "build")   # hérité de la première
        out = self.answer(first["id"], "a")
        self.assertEqual((out["lot"]["moved"], out["lot"]["others"]), (False, 1))
        self.assertEqual(self.lot_state(), "waiting_human")
        out = self.answer(second["id"], "vendredi")
        self.assertTrue(out["lot"]["moved"])
        self.assertEqual(self.lot_state(), "build")

    def test_retrait_par_le_demandeur(self):
        view = self.ask()
        with self.assertRaises(decisions.DecisionError) as ctx:
            decisions.withdraw(self.cfg, self.db, view["id"], requester="dev1")
        self.assertIn("seul son demandeur (orch)", str(ctx.exception))
        out = decisions.withdraw(self.cfg, self.db, view["id"], requester="orch",
                                 why="réglé autrement")
        self.assertEqual((out["decision"]["state"], out["decision"]["withdrawn"]["why"]),
                         ("withdrawn", "réglé autrement"))
        self.assertEqual(self.lot_state(), "build")
        with self.assertRaises(decisions.DecisionError) as ctx:
            self.answer(view["id"], "a")
        self.assertIn("retirée par orch", str(ctx.exception))
        closed = decisions.history(self.db)
        self.assertEqual([(r["id"], r["state"]) for r in closed], [(1, "withdrawn")])


class SignatureTest(_Base):
    def owner_key(self, name="alice", role="owner"):
        registry.upsert(self.db, name, harness="other", host=self.cfg.host)
        private, public, _fp = signing.write_keypair(os.path.join(self.tmp, "cles-" + name),
                                                     name)
        authority.register_key(self.db, name, signing.read_public(public), role=role)
        return private

    def test_reponse_signee_exigee(self):
        view = self.ask(needs_signature=True)
        with self.assertRaises(decisions.DecisionError) as ctx:
            self.answer(view["id"], "a")
        message = str(ctx.exception)
        digest = decisions.answer_digest(view, "a", "a", ALICE)
        self.assertIn("réponse SIGNÉE exigée", message)
        self.assertIn("--hash %s" % digest, message)
        self.assertIn("ameesh decide 1 a --key", message)
        # une clé d'agent ne porte pas l'autorité du propriétaire
        agent_key = self.owner_key("relais", role="agent")
        with self.assertRaises(decisions.DecisionError) as ctx:
            self.answer(view["id"], "a", key_path=agent_key, approver="relais")
        self.assertIn("clé propriétaire", str(ctx.exception))
        # dans le chat : jamais de signature
        with self.assertRaises(decisions.DecisionError) as ctx:
            self.answer(view["id"], "a", channel="chat", key_path=agent_key)
        self.assertIn("pas dans le chat", str(ctx.exception))
        key = self.owner_key()
        out = self.answer(view["id"], "a", key_path=key)
        answer = out["decision"]["answer"]
        self.assertEqual(answer["channel"], "signed")
        self.assertEqual((answer["proof"]["approver"], answer["proof"]["digest"]),
                         ("alice", digest))
        row = self.db.query("SELECT consumed_by FROM mesh_approvals WHERE id = %s",
                            (answer["proof"]["approval_id"],))[0]
        self.assertEqual(row["consumed_by"], "decision:1")
        ok, detail = decisions.verify_proof(self.db, out["decision"])
        self.assertTrue(ok, detail)
        self.assertIn("SIGNÉE", self.db.query(
            "SELECT body FROM agent_mailbox WHERE recipient = 'orch'")[0]["body"])
        # la clé révoquée depuis : la preuve ne vaut plus
        authority.revoke_key(self.db, "alice")
        self.assertFalse(decisions.verify_proof(self.db, decisions.get(self.db, 1))[0])

    def test_signature_preparee_avec_ameesh_approve(self):
        key = self.owner_key()
        view = self.ask(needs_signature=True)
        texte = "b, après la revue"
        digest = decisions.answer_digest(view, texte, None, ALICE)
        proc = self.mesh("approve", "--key", key, "--as", "alice", "--action",
                         decisions.APPROVAL_ACTION, "--kind", decisions.APPROVAL_KIND,
                         "--hash", digest, env=self.mesh_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = self.answer(view["id"], texte, signed=True)
        self.assertEqual(out["decision"]["answer"]["channel"], "signed")
        # une approbation vaut pour SA réponse : une autre demande ne la réutilise pas
        autre = self.ask(needs_signature=True, question="Autre ?")
        with self.assertRaises(decisions.DecisionError):
            self.answer(autre["id"], texte, signed=True)


# ==========================================================================
# qui répond
# ==========================================================================

class IdentiteTest(_Base):
    def test_humain_hors_session_d_agent(self):
        self.assertEqual(decisions.human_session(self.cfg, self.db, **_clean_chain()), ALICE)
        who = decisions.answering_human(self.cfg, self.db, **_clean_chain())
        self.assertEqual((who.human, who.channel), (ALICE, "cli"))

    def test_session_d_agent_refusee(self):
        for env, attendu in (({"AGENT_MAIL_NAME": "orch"}, "celle de l'agent orch"),
                             ({"AMEESH_RUNNER_ID": "r1"}, "menée par l'exécuteur r1")):
            with self.subTest(env=env), mock.patch.dict(os.environ, env), \
                    self.assertRaises(decisions.DecisionError) as ctx:
                decisions.answering_human(self.cfg, self.db, **_clean_chain())
            self.assertIn(attendu, str(ctx.exception))
            self.assertIn("seul un humain répond", str(ctx.exception))

    def test_ancetre_agent_refuse(self):
        """Un agent qui retire AGENT_MAIL_NAME de sa commande reste visible
        dans l'environnement initial de ses ancêtres."""
        envs = {101: {}, 102: {"AGENT_MAIL_NAME": "orch", "AMEESH_RUNNER_ID": "runner-1"}}
        with self.assertRaises(decisions.DecisionError) as ctx:
            decisions.human_session(self.cfg, self.db, chain=[101, 102],
                                    environ_of=lambda pid: envs.get(pid))
        self.assertIn("processus 102 : agent orch mené par runner-1", str(ctx.exception))
        self.assertIsNone(decisions.agent_in_ancestry(chain=[101], environ_of=lambda p: None))

    def test_session_liee_refusee(self):
        registry.upsert(self.db, "externe1", harness="claude", host=self.cfg.host,
                        mode="externe")
        sb.bind(self.cfg, self.db, "externe1", session_id="sess-l124", harness="claude",
                pid=os.getpid(), by=ALICE)
        self.addCleanup(sb.unbind, self.cfg, self.db, session_id="sess-l124",
                        harness="claude", by=ALICE)
        with self.assertRaises(decisions.DecisionError) as ctx:
            decisions.human_session(self.cfg, self.db, **_clean_chain())
        self.assertIn("liée à l'agent externe1", str(ctx.exception))

    def chat_record(self, **over):
        record = {"schema": chat.STATE_SCHEMA, "name": "chat-alice", "human": ALICE,
                  "pid": os.getpid(), "started_at": platform.start_time(os.getpid())}
        record.update(over)
        return chat.write_session(self.cfg, record)

    def chat_env(self, **over):
        env = {"AGENT_MAIL_NAME": "chat-alice", "AMEESH_CHAT": "chat-alice",
               "AMEESH_CHAT_PID": str(os.getpid())}
        env.update(over)
        return mock.patch.dict(os.environ, env)

    def test_chat_verifie(self):
        self.chat_record()
        with self.chat_env():
            who = decisions.answering_human(self.cfg, self.db, **_clean_chain())
        self.assertEqual((who.human, who.channel, who.chat), (ALICE, "chat", "chat-alice"))

    def test_chat_contrefait(self):
        cas = [({}, {}, "session de chat inconnue"),
               ({"pid": os.getpid()}, {"AMEESH_CHAT_PID": "x"}, "sans processus"),
               ({"started_at": 12.0}, {}, "PID réutilisé"),
               ({"human": BOB}, {}, "celui de human:bob"),
               ({}, {"AMEESH_RUNNER_ID": "r1"}, "incohérente"),
               ({}, {"AGENT_MAIL_NAME": "orch"}, "incohérente")]
        for record, env, attendu in cas:
            with self.subTest(attendu=attendu):
                path = self.chat_record(**record) if record else None
                with self.chat_env(**env), self.assertRaises(decisions.DecisionError) as ctx:
                    decisions.answering_human(self.cfg, self.db, **_clean_chain())
                self.assertIn(attendu, str(ctx.exception))
                if path:
                    chat.remove_session(path)
        # le processus du chat n'est pas un ancêtre : refus
        self.chat_record()
        with self.chat_env(), self.assertRaises(decisions.DecisionError) as ctx:
            decisions.answering_human(self.cfg, self.db, chain=[1],
                                      environ_of=lambda pid: {})
        self.assertIn("ne descend pas de la session de chat", str(ctx.exception))
        # un agent au-dessus du chat : refus
        with self.chat_env(), self.assertRaises(decisions.DecisionError) as ctx:
            decisions.answering_human(
                self.cfg, self.db, chain=[os.getpid(), 7],
                environ_of=lambda pid: {"AGENT_MAIL_NAME": "orch"} if pid == 7 else {})
        self.assertIn("processus 7 : agent orch", str(ctx.exception))

    def test_cli_un_agent_ne_repond_pas(self):
        self.ask()
        proc = self.mesh("decide", "1", "a", env=self.mesh_env(AGENT_MAIL_NAME="orch"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("seul un humain répond", proc.stderr)
        proc = self.mesh("decide", "1", "a", env=self.mesh_env(AMEESH_RUNNER_ID="r1"))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(decisions.get(self.db, 1)["state"], "pending")

    @unittest.skipUnless(_humain_hors_agent(), "lancé depuis la session d'un agent : le "
                         "contrôle d'ascendance refuse la réponse (attendu) ; la CI le passe")
    def test_cli_l_humain_repond(self):
        self.ask()
        proc = self.mesh("decide", "1", "b,", "pas", "avant", "lundi", "--json",
                         env=self.mesh_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual((out["decision"]["answer"]["text"], out["decision"]["answer"]["by"],
                          out["decision"]["answer"]["channel"]),
                         ("b, pas avant lundi", ALICE, "cli"))
        proc = self.mesh("decide", "show", "1", env=self.mesh_env())
        self.assertIn("répondue par human:alice (cli)", proc.stdout)
        proc = self.mesh("decisions", "--all", env=self.mesh_env())
        self.assertIn("demandes répondues ou retirées", proc.stdout)


# ==========================================================================
# ameesh chat
# ==========================================================================

class _Popen:
    """Faux lancement du harnais : retient la commande, l'environnement, le
    dossier, et l'état de session vu pendant la conversation."""

    def __init__(self, cfg, calls):
        self.cfg, self.calls = cfg, calls

    def __call__(self, command, cwd=None, env=None):
        state = sorted(os.listdir(chat.session_dir(self.cfg)))
        self.calls.append({"command": command, "cwd": cwd, "env": env, "state": state})
        return self

    def wait(self):
        return 0


class ChatTest(_Base):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_chat(self, *argv, chain=None, environ_of=None):
        args = chat.build_parser().parse_args(list(argv))
        calls: list = []
        clean = _clean_chain()
        self.out, self.err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            code = chat.run(self.cfg, args, chain=chain or clean["chain"],
                            environ_of=environ_of or clean["environ_of"],
                            popen=_Popen(self.cfg, calls))
        return code, calls

    def test_consigne_versionnee(self):
        text = chat.consigne(ALICE, "chat-alice")
        for attendu in ("Tu es **chat-alice**, l'agent de conversation de **human:alice**",
                        "lance d'abord\n   `ameesh decisions`", "ameesh decide <id> <option>",
                        "--new-lot", "aucun geste en production ni irréversible",
                        "tu ne modifies aucun dépôt", "ameesh chat --consigne",
                        "prends jamais la session d'un autre agent"):
            self.assertIn(attendu, text)
        self.assertNotIn("$human", text)
        proc = self.mesh("chat", "--consigne", env=self.mesh_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("**chat-alice**", proc.stdout)

    def test_lancement_sans_prendre_de_session(self):
        self.ask()
        code, calls = self.run_chat()
        self.assertEqual(code, 0)
        call, = calls
        self.assertEqual(call["command"][0], os.path.join(FAKEBIN, "claude"))
        self.assertNotIn("--resume", call["command"])            # une session neuve
        self.assertIn("Tu es **chat-alice**", call["command"][-1])
        env = call["env"]
        self.assertEqual((env["AGENT_MAIL_NAME"], env["AMEESH_CHAT"], env["AMEESH_CHAT_PID"]),
                         ("chat-alice", "chat-alice", str(os.getpid())))
        for key in ("AMEESH_RUNNER_ID", "AMEESH_LEASE_EPOCH", "AGENT_MESH_RUNNER_ID"):
            self.assertNotIn(key, env)
        self.assertEqual(env["AMEESH_SCHEMA"], self.cfg.schema)
        self.assertEqual(call["cwd"], os.path.join(chat.session_dir(self.cfg), "chat-alice"))
        self.assertEqual(call["state"], ["chat-alice", "chat-alice.%d.json" % os.getpid()])
        self.assertFalse(os.path.exists(chat.session_path(self.cfg, "chat-alice", os.getpid())))
        row = registry.get(self.db, "chat-alice")
        self.assertEqual((row["mode"], row["responsible"], row["status"], row["lease_owner"]),
                         ("externe", ALICE, "idle", None))
        self.assertTrue(chat.is_chat_agent(row))
        # aucun autre agent n'a été touché (ni bail, ni session)
        self.assertIsNone(registry.get(self.db, "orch")["lease_owner"])

    def test_refus_depuis_une_session_d_agent(self):
        with mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": "orch"}):
            code, calls = self.run_chat()
        self.assertEqual((code, calls), (1, []))
        self.assertIn("le chat se lance depuis le terminal d'un humain", self.err.getvalue())
        code, calls = self.run_chat(chain=[5], environ_of=lambda pid: {
            "AGENT_MAIL_NAME": "orch", "AMEESH_RUNNER_ID": "attach:x@h"})
        self.assertEqual((code, calls), (1, []))
        self.assertIn("processus 5 : agent orch mené par attach:x@h", self.err.getvalue())
        self.assertIsNone(registry.get(self.db, "chat-alice"))

    def test_un_agent_mene_du_meme_nom_n_est_jamais_pris(self):
        registry.upsert(self.db, "chat-alice", harness="claude", host=self.cfg.host)
        code, calls = self.run_chat()
        self.assertEqual((code, calls), (1, []))
        self.assertEqual(registry.get(self.db, "chat-alice")["mode"], "execute")
        self.assertEqual(self.run_chat("--name", "bavard")[0], 2)

    def test_essai_n_ecrit_rien(self):
        code, calls = self.run_chat("--dry-run")
        self.assertEqual((code, calls), (0, []))
        self.assertIn("aucune variable d'exécuteur", self.out.getvalue())
        self.assertIsNone(registry.get(self.db, "chat-alice"))
        self.assertFalse(os.path.exists(chat.session_dir(self.cfg)))

    def test_hors_des_alertes_de_l_orchestration_et_des_projets(self):
        self.run_chat()
        from ameesh import mail
        mail.send(self.db, "orch", "chat-alice", "C'est transmis à dev1, lot RT-9.")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '2 hours'")
        alertes = exploitation.alerts(self.cfg, self.db)
        self.assertEqual([a for a in alertes if a.get("agent") == "chat-alice"], [])
        listing = storage.of(self.db).operations.listing()
        with mock.patch.object(storage.of(self.db).operations.__class__, "assigners",
                               return_value=["chat-alice", "orch"]):
            self.assertEqual(sous_utilisation.orchestrators(self.cfg, self.db, listing,
                                                            canons=[]), ["orch"])
        self.assertEqual(sous_utilisation.idle_agents(listing, time.time(), 0.0), [])
        view = projects.snapshot(self.db, paid_harnesses=())
        names = [a["name"] for p in view["projects"] for a in p["agents"]]
        self.assertNotIn("chat-alice", names)
        self.assertIn("orch", names)

    @unittest.skipUnless(_humain_hors_agent(), "lancé depuis la session d'un agent : le "
                         "chat refuse de s'y ouvrir (attendu) ; la CI le passe")
    def test_bout_en_bout_reponse_par_le_chat(self):
        self.ask()
        commande = "%s -m ameesh.main decide 1 a --json" % sys.executable
        proc = self.mesh("chat", env=self.mesh_env(AMEESH_TEST_RUN=commande))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        runs = [t["run"] for t in self.turns() if "run" in t]
        self.assertEqual(len(runs), 1, self.turns())
        self.assertEqual(runs[0]["code"], 0, runs[0]["stderr"])
        answer = json.loads(runs[0]["stdout"])["decision"]["answer"]
        self.assertEqual((answer["by"], answer["channel"], answer["text"]),
                         (ALICE, "chat", "a"))
        argv = [t for t in self.turns() if "argv" in t][0]
        self.assertEqual(argv["env"]["AGENT_MAIL_NAME"], "chat-alice")
        self.assertIsNone(argv["env"]["AMEESH_RUNNER_ID"])


# ==========================================================================
# visibilité : alertes, notification, en-tête de ameesh projects
# ==========================================================================

class VisibiliteTest(_Base):
    def test_alerte_puis_relance_a_l_echeance(self):
        now = time.time()
        view = self.ask(urgent=True, due_ts=now + 3600)
        self.ask(question="Autre question ?")
        alertes = [a for a in exploitation.alerts(self.cfg, self.db, now=now + 60)
                   if a["type"].startswith("decision_")]
        self.assertEqual([(a["type"], a["decision"], a["agent"], a["responsible"], a["urgent"])
                          for a in alertes],
                         [("decision_pending", 1, "orch", ALICE, True),
                          ("decision_pending", 2, "orch", ALICE, False)])
        self.assertNotEqual(exploitation.alert_key(alertes[0]),
                            exploitation.alert_key(alertes[1]))
        self.assertIn("répondre : ameesh decide 1", alertes[0]["detail"])
        later = [a for a in exploitation.alerts(self.cfg, self.db, now=now + 3700)
                 if a["type"] == "decision_overdue"]
        self.assertEqual([a["decision"] for a in later], [view["id"]])
        self.assertTrue(later[0]["detail"].startswith("RELANCE : sans réponse"))
        for kind in ("decision_pending", "decision_overdue"):
            self.assertIn(kind, notify.DEFAULT_TYPES)
            self.assertIn(kind, exploitation.ALERT_TYPES)
            self.assertIn(kind, notify.TYPE_LABELS)

    def test_notification_urgente_et_resolution_silencieuse(self):
        sender = notify.DrySender()
        notifier = notify.Notifier(self.cfg, notify.parse_config({"default": ["desktop"]}),
                                   sender=sender, log=lambda text: None)
        view = self.ask(urgent=True)
        records = notifier.run_pass(self.db)
        raised = [r for r in records if r["type"] == "decision_pending"]
        self.assertEqual([(r["human"], r["source"], r["delivery"]) for r in raised],
                         [(ALICE, "alerte", "envoyee")])
        (_canal, message), = sender.sent
        self.assertTrue(message.urgent)
        self.assertIn("décision attendue", message.title)
        self.assertIn("décision #1", message.body)
        self.answer(view["id"], "a")
        records = notifier.run_pass(self.db)
        self.assertEqual([(r["event"], r["type"], r["delivery"]) for r in records],
                         [("resolved", "decision_pending", "sans_envoi")])
        self.assertEqual(len(sender.sent), 1)          # rien de plus n'est envoyé

    def test_en_tete_de_ameesh_projects(self):
        self.ask(urgent=True)
        self.ask(question="Autre ?")
        view = projects.snapshot(self.db, paid_harnesses=(), viewer=ALICE)
        self.assertEqual({k: view["decisions"][k] for k in ("viewer", "pending", "mine",
                                                             "urgent", "overdue")},
                         {"viewer": ALICE, "pending": 2, "mine": 2, "urgent": 1, "overdue": 0})
        first = projects.format_text(view, 120).splitlines()[0]
        self.assertTrue(first.startswith("2 décisions t'attendent, la plus ancienne depuis "),
                        first)
        self.assertIn("(dont 1 urgente)", first)
        autre = projects.snapshot(self.db, paid_harnesses=(), viewer=BOB)
        self.assertEqual(autre["decisions"]["mine"], 0)
        self.assertTrue(projects.format_text(autre, 120).startswith("projets — "))
        agent = projects.snapshot(self.db, paid_harnesses=())
        self.assertTrue(projects.format_text(agent, 120).startswith(
            "2 décisions attendent un humain"))
        proc = self.mesh("projects", "--json", env=self.mesh_env())
        self.assertEqual(json.loads(proc.stdout)["decisions"]["mine"], 2)
        proc = self.mesh("projects", env=self.mesh_env(AGENT_MAIL_NAME="orch"))
        self.assertIn("2 décisions attendent un humain", proc.stdout)


class PlateformeTest(unittest.TestCase):
    """L'environnement initial d'un processus, par la couche plateforme."""

    def test_environnement_d_un_processus(self):
        env = platform.environ()
        self.assertIsInstance(env, dict)
        self.assertIn("PATH", env)
        self.assertIsNone(platform.environ(2 ** 22 + 12345))
        if platform.is_linux():
            with mock.patch.object(platform, "psutil", None):
                self.assertIn("PATH", platform.environ(os.getpid()))
        with mock.patch.object(platform, "psutil", None), \
                mock.patch.object(platform, "SYSTEM", "macos"), \
                self.assertRaises(platform.NotAvailable):
            platform.environ()


if __name__ == "__main__":
    unittest.main()
