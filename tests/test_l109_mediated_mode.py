# SPDX-License-Identifier: AGPL-3.0-only
"""L109 : le mode médié de l'exécuteur et les sessions du harnais dans la VM.

Sans base. L'exécuteur tourne sur une `RemoteDb` dont le transport rejoue
les jeux dorés (`tests/exec_fake.py`) :

* fiche d'hôte (`GET /host`) : hôte, owner `exec:<id>:…`, bail, limites ;
* gardes : ni synchronisation du canon, ni relevé des soldes, ni échéance
  des délégations, ni déplacement entre hôtes, ni connexion à la base ;
* réveil par le flux d'événements ; bail perdu ; serveur injoignable (L72) ;
  exécuteur révoqué ;
* sessions : `mail whoami`, `mail send`, le hook, `work note`, `action
  propose` par le jeton de session, contre un vrai serveur HTTP local.
"""
from __future__ import annotations

from .entrypoint import run as isolated_entrypoint

import contextlib
import dataclasses
import io
import json
import os
import shutil
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import config as config_mod
from ameesh import db as db_mod
from ameesh import registry, storage
from ameesh.mediated_executor import contract as C
from ameesh.mediated_executor import events as E
from ameesh.runner import AgentWorker, Reprise, Runner

from .exec_fake import OWNER, SESSION, Fenced, GoldenServer, GoldenTransport, Seq, make_db


def attendre(cond, delai=5.0, pas=0.01) -> bool:
    fin = time.monotonic() + delai
    while time.monotonic() < fin:
        if cond():
            return True
        time.sleep(pas)
    return cond()


class MedieBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l109-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        #: config VIDE mais existante : un chemin explicite absent est refusé (#1127)
        self.config_vide = os.path.join(self.tmp, "config-vide.json")
        with open(self.config_vide, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        self.cfg = dataclasses.replace(
            config_mod.Config(), backend="mediated", exec_url="https://mesh.exemple",
            state_dir=os.path.join(self.tmp, "etat"), v0_state=os.path.join(self.tmp, "v0"),
            config_dir=os.path.join(self.tmp, "conf"), container_runtime="none",
            canon=os.path.join(self.tmp, "canon"), budget_usd_per_hour=0.0, poll=1.0,
            db_retry_max=0.2, resource_interval=0.0, worktree_roots=(self.tmp,))
        self.transport = GoldenTransport()
        self.responder = self.transport.responder
        self.responder.script["agents.runner_seen"] = None  # owner choisi par le runner
        self.responder.script["agents.resolve_name"] = lambda body: (
            body["args"][0] if body.get("args") else body["kwargs"]["name"])
        self.db = make_db(self.transport, cfg=self.cfg)
        relay_health = mock.patch("ameesh.relay.credential_ready", return_value=True)
        self.relay_health = relay_health.start()
        self.addCleanup(relay_health.stop)
        patch = mock.patch.object(Reprise, "MINIMUM", 0.02)
        patch.start()
        self.addCleanup(patch.stop)
        # aucune connexion à une base n'est permise en mode médié
        interdit = mock.patch.object(db_mod, "PsycopgDriver",
                                     side_effect=AssertionError("connexion à la base"))
        interdit.start()
        self.addCleanup(interdit.stop)
        interdit2 = mock.patch.object(db_mod, "PsqlDriver",
                                      side_effect=AssertionError("connexion à la base"))
        interdit2.start()
        self.addCleanup(interdit2.stop)

    def runner(self, **kw) -> Runner:
        return Runner(self.cfg, self.db, **kw)


class FicheHoteTest(MedieBase):
    def test_hote_owner_bail_et_limites_viennent_du_serveur(self):
        runner = self.runner()
        self.assertTrue(runner.mediated)
        self.assertEqual(runner.host, "anna-portable")
        self.assertEqual(runner.cfg.host, "anna-portable")
        self.assertEqual(runner.runner_id, "exec:7f3a9c2e4b1d6058:anna-portable:%d" % os.getpid())
        self.assertEqual(runner.lease_ttl, 90.0)
        self.assertEqual(runner._host_max_agents, 1)
        self.assertFalse(runner.relocate)

    def test_mode_postgres_inchange(self):
        runner = Runner(dataclasses.replace(self.cfg, backend="auto"), object())
        self.assertFalse(runner.mediated)
        self.assertEqual(runner.host, self.cfg.host)
        self.assertIsNone(runner.host_info)


class GardesTest(MedieBase):
    def test_pas_de_canon_de_solde_ni_de_delegation(self):
        runner = self.runner()
        with mock.patch("ameesh.work.expire_delegations",
                        side_effect=AssertionError("échéance au serveur")), \
                mock.patch("ameesh.canon_sync.sync", side_effect=AssertionError("canon")):
            self.assertTrue(runner.canon_sync_once())
            runner.start_canon_sync()
            self.assertIsNone(runner.canon_thread)
            self.assertEqual(runner.balance_once(), 0)
            self.assertEqual(runner.expire_delegations_once(), [])
        self.assertEqual(self.transport.calls, [])

    def test_pas_de_deplacement_entre_hotes(self):
        runner = self.runner()
        runner.relocate = True  # même forcé
        worker = types.SimpleNamespace(runner=runner)
        self.assertFalse(AgentWorker.maybe_relocate(worker, {"blocked": True}))

    def test_releve_des_ressources_par_le_serveur(self):
        self.responder.script["hosts.turns_in_progress"] = 0
        self.responder.script["hosts.record"] = {"host": "anna-portable"}
        runner = self.runner()
        runner.resource_once()
        self.assertIn("hosts.record", self.transport.ops())
        record = [b for _r, b, _k in self.transport.calls if b["op"] == "hosts.record"][0]
        self.assertEqual(record["args"][0]["host"], "anna-portable")

    def test_porte_fermee_aucune_reclamation(self):
        from ameesh.mediated_executor import gate
        runner = self.runner()
        # L112 dans L109 : la porte relayée au serveur est celle du contrôleur
        runner.host_gate.gate = mock.Mock(state=lambda: gate.GateState("draining", 3))
        self.responder.script["leases.reap"] = []
        with mock.patch.object(runner, "_note_gate_wait") as note:
            runner.sweep()
        note.assert_called_once_with("host gate: " + runner.host_gate.describe())
        self.assertIn("agents.runner_seen", self.transport.ops())
        self.assertNotIn("agents.claimable", self.transport.ops())
        self.assertNotIn("leases.claim", self.transport.ops())

    def test_passe_reclame_avec_l_owner_de_l_executeur(self):
        self.responder.script.update({
            "leases.reap": [], "agents.claimable": [{"name": "inge-front"}],
            "leases.claim": {"name": "inge-front", "lease_owner": "x", "lease_epoch": 42,
                             "lease_expires_ts": time.time() + 90}})
        runner = self.runner(once=True, dry_run=True)
        with mock.patch.object(AgentWorker, "process_once", return_value=False), \
                mock.patch.object(AgentWorker, "release_lease"), \
                mock.patch.object(AgentWorker, "_compta_en_attente", return_value=None):
            runner.sweep()
        claim = [b for _r, b, _k in self.transport.calls if b["op"] == "leases.claim"][0]
        self.assertEqual(claim["args"][:2], ["inge-front", runner.runner_id])
        self.assertEqual(claim["args"][2], 90.0)
        claimable = [b for _r, b, _k in self.transport.calls
                     if b["op"] == "agents.claimable"][0]
        self.assertEqual(claimable["args"][0], "anna-portable")

    def test_register_refuse(self):
        from ameesh import runner as runner_mod
        env = {"AMEESH_BACKEND": "mediated", "AMEESH_EXEC_URL": "https://mesh.exemple",
               "AMEESH_CONFIG": self.config_vide}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(isolated_entrypoint(runner_mod.main, ["register", "x", "dsh"]), 2)


class ReveilParLeFluxTest(MedieBase):
    def _ecoute(self, runner):
        thread = threading.Thread(target=runner._listen_loop, daemon=True)
        thread.start()

        def arret():
            runner.stop.set()
            if runner.listener:
                runner.listener.close()
            thread.join(timeout=5)
        self.addCleanup(arret)
        return thread

    def test_un_courrier_reveille_son_worker(self):
        runner = self.runner()
        worker = types.SimpleNamespace(wake=threading.Event(), interrupt_allowed=lambda r: False,
                                       request_preempt=lambda: None)
        runner.workers["inge-front"] = worker
        self.transport.events = [E.Event("k3f9:17", "agent_mail", {"to": "inge-front",
                                                                   "id": 812})]
        self._ecoute(runner)
        self.assertTrue(attendre(worker.wake.is_set), "le flux n'a pas réveillé le worker")
        # le réveil ne fait pas foi : le message est relu par l'API
        self.assertIn("mailbox.get", self.transport.ops())
        self.assertEqual(self.db.events_cursor, "k3f9:17")

    def test_trou_de_reprise_reveille_tous_les_workers(self):
        runner = self.runner()
        workers = [types.SimpleNamespace(wake=threading.Event()) for _ in range(2)]
        runner.workers.update({"a": workers[0], "b": workers[1]})
        self.transport.events = [E.Event("k3f9:19", E.RESET, {"reason": "gap"})]
        self._ecoute(runner)
        self.assertTrue(attendre(lambda: all(w.wake.is_set() for w in workers)))

    def test_coupure_du_flux_reabonnement(self):
        runner = self.runner()
        self._ecoute(runner)
        self.assertTrue(attendre(lambda: len(self.transport.stream_calls) >= 2, delai=8.0),
                        "l'écoute doit se réabonner après une coupure")


class BailEtPanneTest(MedieBase):
    def _worker(self) -> AgentWorker:
        runner = self.runner()
        self.responder.script["pending_spend.get"] = None
        lease = {"name": "inge-front", "lease_owner": runner.runner_id, "lease_epoch": 42,
                 "lease_expires_ts": time.time() + 90}
        self.db.book.hold("inge-front", runner.runner_id, 42)
        agent = {"name": "inge-front", "harness": "dsh", "cwd": self.tmp, "status": "queued"}
        return AgentWorker(runner, agent, lease)

    def test_reglement_sans_reservation_locale_ne_bloque_pas_le_marqueur(self):
        """#1469 : le backend médié n'a pas d'enveloppe de compte locale."""
        worker = self._worker()
        # r2 : le marqueur n'est effacé que s'il est encore celui de ce tour
        created_at = "2026-10-20T21:00:00.000000Z"
        spend_key = "inge-front:0:" + created_at
        self.responder.script["pending_spend.get"] = {
            "agent": "inge-front", "start_index": 0, "turn": "prompt", "model": "m",
            "created_at": created_at, "created_ts": 123.0, "spend_key": spend_key}
        self.responder.script["pending_spend.clear"] = True
        usage = types.SimpleNamespace(reread_tokens=0, session=None)
        with mock.patch("ameesh.cost.CostBook") as book:
            book.return_value.record.return_value = usage
            self.assertTrue(worker._compta_ecrit({
                "start": 0, "created_at": created_at, "created_ts": 123.0,
                "spend_key": spend_key, "turn": "prompt", "model": "m"}))
        self.assertFalse(worker._compta_en_echec)
        self.assertIn("pending_spend.clear", self.transport.ops())
        self.assertFalse(any(op.startswith("budgets.") for op in self.transport.ops()))

    def test_provider_health_stays_on_the_relay_server(self):
        worker = self._worker()
        with mock.patch("ameesh.auth_failure.failing",
                        side_effect=AssertionError("provider health read on device")):
            self.assertTrue(worker.auth_ready())
            self.assertTrue(worker._rotation_allowed())
        self.assertNotIn("credentials.health", self.transport.ops())
        with self.assertRaises(C.NotSupportedRemotely):
            self.db.storage.credentials.report("anna-portable", "dsh", "", "failing",
                                             source="turn")

    def test_relay_health_blocks_and_recovers_without_local_credentials(self):
        worker = self._worker()
        worker.agent["harness"] = "deepseek"
        self.responder.script["leases.set_marked_block"] = "done"
        self.responder.script["leases.clear_marked_block"] = "done"
        self.relay_health.return_value = False
        with mock.patch("ameesh.auth_failure.failing", side_effect=AssertionError("local health")):
            self.assertFalse(worker.auth_ready())
            self.assertEqual(worker._auth_status, "auth_blocked: deepseek/relay")
            self.assertIn("leases.set_marked_block", self.transport.ops())
            self.relay_health.return_value = True
            self.assertTrue(worker.auth_ready())
        self.assertEqual(worker._auth_status, "")
        self.assertIn("leases.clear_marked_block", self.transport.ops())
        self.assertFalse(any(op.startswith("credentials.") for op in self.transport.ops()))

    def test_failed_relay_turn_does_not_count_as_fast_failure(self):
        worker = self._worker()
        worker.agent["harness"] = "deepseek"
        self.responder.script["leases.set_marked_block"] = "done"
        self.responder.script["leases.end_turn"] = True
        self.relay_health.return_value = False
        worker.fast_failure = True
        self.assertTrue(worker._end_relay_auth_failure(0))
        self.assertFalse(worker.fast_failure)
        self.assertEqual(self.transport.ops().count("leases.end_turn"), 1)
        self.assertFalse(any(op.startswith("credentials.") for op in self.transport.ops()))

    def test_healthy_relay_does_not_relabel_another_block_after_failure(self):
        worker = self._worker()
        worker.agent["harness"] = "deepseek"
        worker._auth_status = "auth_blocked: old-account"
        worker.fast_failure = True
        self.assertFalse(worker._end_relay_auth_failure(0))
        self.assertTrue(worker.fast_failure)
        self.assertEqual(worker._auth_status, "auth_blocked: old-account")
        self.assertNotIn("leases.end_turn", self.transport.ops())

    def test_bail_perdu_au_battement(self):
        worker = self._worker()
        self.responder.script["leases.renew"] = Fenced()
        self.assertFalse(worker.renew())
        self.assertTrue(worker.lease_lost.is_set())
        self.assertIsNone(self.db.book.fence("inge-front"))
        avant = len(self.transport.calls)
        registry.set_status(self.db, "inge-front", "idle", status_text="x")
        self.assertFalse(any(C.load().get(body["op"]).write
                             for _route, body, _key in self.transport.calls[avant:]),
                         "plus aucune écriture sans bail")

    def test_serveur_injoignable_puis_retabli_l72(self):
        worker = self._worker()
        self.responder.script["leases.end_turn"] = Seq(
            db_mod.Unavailable("serveur injoignable"), db_mod.Unavailable("toujours"), True)
        self.responder.script["leases.renew"] = time.time() + 90
        ok = worker._avec_reprise("fin de tour", registry.end_turn, self.db, "inge-front",
                                  worker.runner.runner_id, 42, status="idle")
        self.assertTrue(ok)
        self.assertEqual(self.transport.ops().count("leases.end_turn"), 3)

    def test_passe_principale_survit_au_serveur_injoignable(self):
        runner = self.runner()
        panne = db_mod.Unavailable("serveur injoignable")
        self.responder.script["leases.reap"] = Seq(panne, panne, panne, [])
        self.responder.script["agents.claimable"] = []
        thread = threading.Thread(target=runner.run, daemon=True)
        with mock.patch.object(runner, "poll", 0.05):
            thread.start()
            self.assertTrue(attendre(lambda: "agents.claimable" in self.transport.ops(),
                                     delai=8.0), "la passe doit reprendre après la panne")
            runner.stop.set()
            runner.wake_all.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.transport.ops().count("leases.reap") >= 4, True)

    def test_executeur_revoque_s_arrete(self):
        runner = self.runner()
        self.responder.script["leases.reap"] = C.ExecutorRevoked("exécuteur révoqué")
        thread = threading.Thread(target=runner.run, daemon=True)
        thread.start()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "un exécuteur révoqué doit s'arrêter")
        self.assertEqual(self.transport.ops().count("leases.reap"), 1, "sans nouvel essai")

    def test_environnement_du_harnais(self):
        worker = self._worker()
        env = {"AMEESH_DSN": "postgresql://secret", "AGENT_MESH_DSN": "x"}
        worker._mediated_env(env)
        self.assertNotIn("AMEESH_DSN", env)
        self.assertNotIn("AGENT_MESH_DSN", env)
        self.assertEqual(env["AMEESH_BACKEND"], "mediated")
        self.assertEqual(env["AMEESH_EXEC_URL"], "https://mesh.exemple")
        self.assertEqual(env["AMEESH_EXEC_TOKEN"], SESSION)
        self.assertEqual(self.transport.session_tokens,
                         [C.Fence("inge-front", worker.runner.runner_id, 42)])

    def test_environnement_sans_jeton_si_serveur_injoignable(self):
        worker = self._worker()
        with mock.patch.object(self.transport, "session_token",
                               side_effect=db_mod.Unavailable("x")):
            env = {}
            worker._mediated_env(env)
        self.assertNotIn("AMEESH_EXEC_TOKEN", env)


# --------------------------------------------------------------------------
# sessions du harnais dans la VM (jeton de session, vrai serveur HTTP local)
# --------------------------------------------------------------------------

class SessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l109-session-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.server = GoldenServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        self.responder = self.server.responder
        self.responder.script["agents.resolve_name"] = lambda body: (
            body["args"][0] if body.get("args") else body["kwargs"]["name"])
        self.responder.script["leases.state"] = {
            "lease_owner": OWNER, "lease_epoch": 42, "status": "running", "live": True}
        #: config VIDE mais existante : un chemin explicite absent est refusé (#1127)
        config_vide = os.path.join(self.tmp, "config-vide.json")
        with open(config_vide, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        env = {
            "AMEESH_CONFIG": config_vide,
            "AMEESH_STATE": os.path.join(self.tmp, "etat"),
            "AGENT_MAIL_STATE": os.path.join(self.tmp, "v0"),
            "AGENT_MAIL_CONFIG": os.path.join(self.tmp, "conf"),
            "AMEESH_THREADS": os.path.join(self.tmp, "fils"),
            "AMEESH_BACKEND": "mediated", "AMEESH_EXEC_URL": self.server.url,
            "AMEESH_EXEC_TOKEN": SESSION, "AGENT_MAIL_NAME": "inge-front",
            "AMEESH_RUNNER_ID": OWNER, "AMEESH_LEASE_EPOCH": "42",
            "AMEESH_HARNESS": "claude",  # fixture, never inherit the developer's harness
        }
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("AMEESH_DSN", "AGENT_MESH_DSN", "AGENT_MESH_RUNNER_ID",
                     "AGENT_MESH_LEASE_EPOCH", "AGENT_MESH_BACKEND"):
            os.environ.pop(name, None)

    def ameesh(self, *argv, stdin: str = "") -> tuple[int, str, str]:
        from ameesh import main as main_mod
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin)):
            code = isolated_entrypoint(main_mod.main, list(argv))
        return code, out.getvalue(), err.getvalue()

    def session_requests(self) -> list:
        return [r for r in self.server.requests if r["path"].endswith("/session/op")]

    def assert_session_only(self) -> None:
        self.assertTrue(self.server.requests)
        for req in self.server.requests:
            self.assertTrue(req["path"].endswith("/session/op"), req["path"])
            self.assertEqual(req["headers"]["Authorization"], "Bearer " + SESSION)

    def test_whoami_lie_au_bail(self):
        code, out, err = self.ameesh("mail", "whoami")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "inge-front [runner]")
        self.assertEqual(self.server.ops(), ["leases.state"])
        self.assert_session_only()

    def test_whoami_bail_d_un_autre_epoch(self):
        self.responder.script["leases.state"] = {
            "lease_owner": OWNER, "lease_epoch": 43, "status": "running", "live": True}
        code, _out, err = self.ameesh("mail", "whoami")
        self.assertEqual(code, 1)
        self.assertIn("epoch", err)

    def test_mail_send(self):
        self.responder.script["mailbox.send"] = {
            "id": 813, "created_ts": 1791640000.12, "sender_project": "site",
            "recipient_project": "site"}
        # #515 : le fil d'un message lié à un lot suit le projet du lot ; la
        # session lit ce projet par `work.get` (lecture bornée du contrat 1.1),
        # après le dépôt et avant l'indexation du fil. Le lot scripté porte un
        # projet autre que celui de l'expéditeur : c'est bien celui du lot qui
        # range le message.
        self.responder.script["work.get"] = {"id": 812, "app": "canon"}
        self.responder.script["threads.index"] = None
        # L361c : le compte du fil vient du census, porté par `threads.recount`
        self.responder.script["threads.recount"] = 1791640000
        code, out, err = self.ameesh("mail", "send", "coord", "PR prête pour relecture.",
                                     "--lot", "812")
        self.assertEqual(code, 0, err)
        self.assertIn("coord", out)
        # contrat 1.1 : le fil est indexé depuis la session, auteur `agent:<nom>`
        self.assertEqual(self.server.ops(), ["leases.state", "agents.resolve_name",
                                             "agents.resolve_name", "agents.overview",
                                             "agents.resolve_name", "agents.resolve_name",
                                             "mailbox.send", "work.get", "agents.resolve_name",
                                             "agents.resolve_name", "threads.index",
                                             "threads.recount"])
        index = [r for r in self.session_requests() if r["body"]["op"] == "threads.index"][0]
        self.assertEqual(index["body"]["kwargs"]["author"], "agent:inge-front")
        self.assertEqual(index["body"]["kwargs"]["project"], "canon")
        self.assertEqual(index["body"]["kwargs"]["lot"], "812")
        self.assertIs(index["body"]["kwargs"]["count"], False)
        self.assertEqual(index["body"]["fence"]["epoch"], 42)
        # L361c : le census du fil est porté par la même session, sous le même lot
        recount = [r for r in self.session_requests()
                   if r["body"]["op"] == "threads.recount"][0]
        self.assertEqual(recount["body"]["kwargs"]["project"], "canon")
        self.assertEqual(recount["body"]["kwargs"]["lot"], "812")
        self.assertIsInstance(recount["body"]["kwargs"]["kept"], int)
        self.assertIsInstance(recount["body"]["kwargs"]["end"], int)
        req = [r for r in self.session_requests() if r["body"]["op"] == "mailbox.send"][-1]
        self.assertEqual(req["body"]["args"][:2], ["inge-front", "coord"])
        self.assertTrue(req["headers"].get(C.IDEMPOTENCY_HEADER))
        self.assert_session_only()

    def test_mail_send_lot_d_un_autre_projet_retombe_sur_l_expediteur(self):
        # #515 : `work.get` est borné, sous jeton de session, au lot de l'agent
        # (le sien, un lot qui lui est délégué, ou le projet de son équipe) ;
        # le lot d'un autre projet se lit `null` — le fil garde alors la règle
        # historique (projet de l'expéditeur), sans échouer ni perdre le
        # message.
        self.responder.script["mailbox.send"] = {
            "id": 814, "created_ts": 1791640000.12, "sender_project": "site",
            "recipient_project": "canon"}
        self.responder.script["work.get"] = None
        self.responder.script["threads.index"] = None
        self.responder.script["threads.recount"] = 1791640000
        code, out, err = self.ameesh("mail", "send", "coord", "Lot d'un autre projet.",
                                     "--lot", "900")
        self.assertEqual(code, 0, err)
        self.assertIn("coord", out)
        self.assertIn("work.get", self.server.ops())
        index = [r for r in self.session_requests() if r["body"]["op"] == "threads.index"][0]
        self.assertEqual(index["body"]["kwargs"]["project"], "site")
        self.assertEqual(index["body"]["kwargs"]["lot"], "900")
        self.assert_session_only()

    def test_new_work_item_refused_before_mail_is_sent(self):
        code, _out, err = self.ameesh("mail", "send", "coord", "Review the change.",
                                     "--new-lot", "Review", "--estimate", "1h")
        self.assertEqual(code, 2)
        self.assertIn("mediated contract 1.1", err)
        self.assertEqual(self.server.ops(), ["leases.state", "agents.resolve_name",
                                             "agents.resolve_name", "agents.overview"])
        self.assert_session_only()

    def test_hook_remet_par_la_session(self):
        # contrat 1.1 : réservation du hook avec le jeton de session ; pas
        # d'inscription (l'exécuteur a tout écrit sous son bail)
        self.responder.script["mailbox.reserve"] = []
        code, out, _err = self.ameesh("mail", "hook", "claude",
                                      stdin=json.dumps({"hook_event_name": "Stop",
                                                        "session_id": "s1"}))
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("mailbox.reserve", self.server.ops())
        self.assertNotIn("agents.upsert", self.server.ops())
        reserve = [r for r in self.session_requests() if r["body"]["op"] == "mailbox.reserve"]
        self.assertEqual(reserve[0]["body"]["fence"],
                         {"agent": "inge-front", "owner": OWNER, "epoch": 42})
        self.assert_session_only()

    def test_foreign_harness_hook_leaves_mediated_mail_pending(self):
        self.responder.script["mailbox.reserve"] = []
        with mock.patch.dict(os.environ, {"AMEESH_HARNESS": "claude"}):
            code, out, err = self.ameesh("mail", "hook", "codex", stdin=json.dumps({
                "hook_event_name": "Stop", "session_id": "foreign-session"}))
        self.assertEqual((code, out), (0, ""))
        self.assertIn("codex", err)
        self.assertNotIn("mailbox.reserve", self.server.ops())
        self.assertNotIn("mailbox.deliver", self.server.ops())
        self.assertNotIn("agents.upsert", self.server.ops())
        self.assert_session_only()

    def test_hook_bail_invalide_ne_touche_a_rien(self):
        self.responder.script["leases.state"] = {
            "lease_owner": "exec:autre:h:1", "lease_epoch": 42, "status": "running",
            "live": True}
        code, out, err = self.ameesh("mail", "hook", "claude",
                                     stdin=json.dumps({"hook_event_name": "Stop"}))
        self.assertEqual((code, out), (0, ""))
        self.assertIn("unbound", err)

    def test_inbox_par_la_session(self):
        # contrat 1.1 : `mailbox.unread` est ouverte au jeton de session
        self.responder.script["mailbox.unread"] = []
        code, _out, err = self.ameesh("mail", "inbox")
        self.assertEqual(code, 0, err)
        self.assertIn("mailbox.unread", self.server.ops())
        self.assert_session_only()

    def test_work_note_et_move(self):
        # les états des jeux dorés (`doing`, `review`) ne sont pas ceux de
        # `work.STATES` : lot scripté dans un état réel
        # B5 (#1922) : le vrai `work.get` porte `workflow_ref` ('' pour un lot
        # non pinné) ; la garde des commandes historiques le lit dans
        # l'élément, sans SELECT brut — interdit sur une connexion médiée.
        lot = {"id": 812, "project": "site", "title": "Page d'accueil", "state": "build",
               "assignee": "inge-front", "loops": 0, "workflow_ref": ""}
        self.responder.script["work.get"] = lot
        self.responder.script["work.move"] = dict(lot, state="qa")
        self.responder.script["work.note"] = None
        code, out, err = self.ameesh("work", "note", "812", "Tests verts.", "--actor",
                                     "inge-front")
        self.assertEqual(code, 0, err)
        self.assertIn("812", out)
        code, out, err = self.ameesh("work", "move", "812", "qa", "--note", "PR ouverte",
                                     "--actor", "inge-front")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.server.ops(), ["agents.resolve_name", "work.get", "work.note",
                                             "agents.resolve_name", "work.get", "work.move"])
        self.assert_session_only()

    def test_work_move_lot_pinne_refuse_sans_sql(self):
        # B5 (#1922) : un lot pinné sur une révision Process ne bouge que par
        # le registre ; la session le refuse depuis l'élément de `work.get`,
        # sans SELECT brut et sans envoyer `work.move`.
        lot = {"id": 815, "project": "site", "title": "Page d'accueil",
               "state": "build", "assignee": "inge-front", "loops": 0,
               "workflow_ref": "canon/process/site@7"}
        self.responder.script["work.get"] = lot
        code, _out, err = self.ameesh("work", "move", "815", "qa", "--actor", "inge-front")
        self.assertEqual(code, 1)
        self.assertIn("pinned to workflow canon/process/site@7", err)
        self.assertIn("process_registry", err)
        self.assertEqual(self.server.ops(), ["agents.resolve_name", "work.get"])
        self.assert_session_only()

    def test_work_close_lot_pinne_refuse_sans_sql(self):
        # même garde pour la fermeture d'un lot pinné (#1922) : la session la
        # refuse depuis la lecture, pas depuis un SELECT brut.
        lot = {"id": 816, "project": "site", "title": "Page d'accueil",
               "state": "build", "assignee": "inge-front", "loops": 0,
               "workflow_ref": "canon/process/site@7"}
        self.responder.script["work.get"] = lot
        code, _out, err = self.ameesh("work", "close", "816", "--abandoned",
                                      "--actor", "inge-front")
        self.assertEqual(code, 1)
        self.assertIn("pinned to workflow canon/process/site@7", err)
        self.assertEqual(self.server.ops(), ["agents.resolve_name", "work.get"])
        self.assert_session_only()

    def test_action_propose(self):
        proposees: list = []

        def propose(body):
            proposees.append(body["kwargs"])
            return None

        def lire(_body):
            k = proposees[-1]
            return {"action_id": k["action_id"], "project": k["project"],
                    "connector": k["connector"], "operation": k["operation"],
                    "target": k["target"], "args": json.loads(k["args_json"]),
                    "amount": k["amount"], "currency": k["currency"],
                    "policy_version": k["policy_version"], "state": "proposed",
                    "class": k["action_class"], "requires_receipt": k["requires_receipt"],
                    "approvers": [], "work_item": k["work_item"], "attempts": 0,
                    "digest": k["digest"]}

        self.responder.script["actions.propose"] = propose
        self.responder.script["actions.get"] = lire
        code, out, err = self.ameesh(
            "action", "propose", "--connector", "shell-noop", "--operation", "write",
            "--project", "site", "--target", "note.txt", "--args", '{"text":"bonjour"}')
        self.assertEqual(code, 0, err)
        self.assertIn("proposed", out)
        self.assertEqual(proposees[0]["proposed_by"], "agent:inge-front")
        self.assertIn("actions.propose", self.server.ops())
        self.assert_session_only()


if __name__ == "__main__":
    unittest.main()
