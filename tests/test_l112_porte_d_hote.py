# SPDX-License-Identifier: AGPL-3.0-only
"""L112 : l'exécuteur obéit à l'état d'inactivité de l'hôte (porte d'hôte).

* `FileGate` (inotify ou sondage) et `SocketGate` : lecture, état de repli,
  acquittement ;
* le contrôleur : chaque transition, échéance du retrait, acquittement
  `drained`, retard ;
* l'exécuteur réel (base de test, faux harnais à appels d'outils) : retrait
  pendant un tour (arrêt au point sûr), échéance dépassée, arrêt, état
  illisible (hôte médié et classique), reprise automatique, hôte classique
  inchangé ;
* serveur : corps de disponibilité, registre, alertes poussées par notify ;
* bail d'un hôte volatil (fiche Host).
"""
from __future__ import annotations

import dataclasses
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from ameesh import canon, config as config_mod, db as db_mod, exploitation, notify, registry
from ameesh.executeur_mediee import disponibilite as D
from ameesh.executeur_mediee import porte as P
from ameesh.executeur_mediee import porte_hote as H
from ameesh.runner import Runner

from .support import PgTestCase, child_env
from .test_l43_perimetre_hote import HOST, _Canons, host

GOLDEN = os.path.join(os.path.dirname(__file__), "dore", "executeur_mediee", "porte.json")


def attendre(cond, timeout: float = 15.0, pas: float = 0.05):
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        valeur = cond()
        if valeur:
            return valeur
        time.sleep(pas)
    return cond()


def etat(state: str, seq: int, **extra) -> dict:
    return dict({"schema": "ameesh-host-state/1", "state": state, "seq": seq,
                 "until_ts": None, "drain_deadline_ts": None, "caps": {},
                 "reason": "idle" if state == "available" else "user_active"}, **extra)


def ecrit_etat(path: str, data) -> None:
    """Comme le runner Compute : fichier temporaire puis `rename`."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data))
    os.replace(tmp, path)


# ==========================================================================
# 1. FileGate et SocketGate
# ==========================================================================

class FileGateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ameesh-l112-")
        self.path = os.path.join(self.dir, "state.json")

    def gate(self, **kw):
        g = H.FileGate(self.path, **kw)
        self.addCleanup(g.close)
        return g

    def test_absent_illisible_et_lisible(self):
        g = self.gate()
        self.assertEqual(g.state().state, "stopped")         # absent : médié
        self.assertEqual(g.state().reason, "unknown")
        ecrit_etat(self.path, etat("available", 4, caps={"max_concurrent": 1}))
        st = g.state()
        self.assertEqual((st.state, st.seq, st.caps["max_concurrent"]), ("available", 4, 1))
        for mauvais in ("{pas du json", json.dumps(etat("sleeping", 5)),
                        json.dumps(etat("available", 6, drain_deadline_ts="bientôt")),
                        json.dumps(etat("available", 7, caps={"max_concurrent": -1})),
                        json.dumps(dict(etat("available", 8), schema="ameesh-host-state/0"))):
            ecrit_etat(self.path, mauvais)
            st = g.state()
            self.assertEqual(st.state, "stopped", mauvais)
            self.assertEqual(st.seq, 4)  # dernier seq lisible, gardé

    def test_repli_d_un_hote_classique(self):
        g = self.gate(fallback="available")
        self.assertEqual(g.state().state, "available")
        ecrit_etat(self.path, "illisible")
        self.assertEqual(g.state().state, "available")

    def test_etats_dores_lus(self):
        with open(GOLDEN, encoding="utf-8") as fh:
            golden = json.load(fh)
        g = self.gate()
        for d in golden["etats"]:
            ecrit_etat(self.path, d)
            self.assertEqual(g.state().to_json(), d)
        for d in golden["illisibles"]:
            ecrit_etat(self.path, d)
            self.assertEqual(g.state().state, "stopped")

    def _changement(self, **kw):
        g = self.gate(**kw)
        ecrit_etat(self.path, etat("available", 1))
        self.assertEqual(g.wait_change(0.1).seq, 1)
        self.assertEqual(g.wait_change(0.1).seq, 1)  # inchangé : rendu au délai
        threading.Timer(0.2, ecrit_etat, (self.path, etat("draining", 2))).start()
        debut = time.monotonic()
        st = g.wait_change(5.0)
        return st, time.monotonic() - debut

    def test_changement_vu_par_inotify(self):
        st, duree = self._changement(poll=30.0)  # sans inotify, rien avant 30 s
        self.assertEqual((st.state, st.seq), ("draining", 2))
        self.assertLess(duree, 2.0)

    def test_changement_vu_par_sondage(self):
        st, duree = self._changement(use_inotify=False, poll=0.1)
        self.assertEqual((st.state, st.seq), ("draining", 2))
        self.assertLess(duree, 2.0)

    def test_acquittement_ecrit_atomiquement(self):
        g = self.gate()
        g.acknowledge(P.GateAck(seq=3, state="draining", in_turn=("a",), held=("a",)))
        with open(os.path.join(self.dir, "ack.json"), encoding="utf-8") as fh:
            ack = P.GateAck.from_json(json.load(fh))
        self.assertEqual((ack.seq, ack.state, ack.held, ack.drained), (3, "draining", ("a",),
                                                                       False))
        self.assertGreater(ack.ts, 0)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])

    def test_acquittement_sans_dossier_ne_leve_pas(self):
        vus = []
        g = H.FileGate(os.path.join(self.dir, "absent", "state.json"), log=vus.append)
        g.acknowledge(P.GateAck(seq=1, state="available"))
        self.assertTrue(vus)
        g.close()


class _RunnerCompute:
    """Faux runner Compute : socket Unix, pousse des lignes, lit les acks."""

    def __init__(self, path: str):
        self.path = path
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(1)
        self.conn = None
        self.acks: list[dict] = []

    def accepte(self):
        self.srv.settimeout(5.0)
        self.conn, _ = self.srv.accept()
        threading.Thread(target=self._lit, args=(self.conn,), daemon=True).start()

    def _lit(self, conn):
        try:
            for line in conn.makefile("rb"):
                self.acks.append(json.loads(line))
        except OSError:
            pass

    def pousse(self, data):
        line = data if isinstance(data, str) else json.dumps(data)
        self.conn.sendall((line + "\n").encode())

    def coupe(self):
        self.conn.shutdown(socket.SHUT_RDWR)
        self.conn.close()

    def ferme(self):
        self.srv.close()


class SocketGateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ameesh-l112-s-")
        self.path = os.path.join(self.dir, "gate.sock")
        self.compute = _RunnerCompute(self.path)
        self.addCleanup(self.compute.ferme)

    def test_etats_acks_coupure_et_reconnexion(self):
        g = H.SocketGate(self.path, lost_grace=0.3, retry=0.05)
        self.addCleanup(g.close)
        self.assertEqual(g.state().state, "stopped")  # rien reçu : repli
        self.compute.accepte()
        self.compute.pousse(etat("available", 1, caps={"max_concurrent": 2}))
        st = attendre(lambda: g.state().state == "available" and g.state())
        self.assertEqual(st.caps, {"max_concurrent": 2})
        g.acknowledge(P.GateAck(seq=1, state="available"))
        self.assertTrue(attendre(lambda: self.compute.acks))
        self.assertEqual(self.compute.acks[0]["schema"], "ameesh-host-ack/1")
        self.compute.pousse("{illisible")
        self.assertTrue(attendre(lambda: g.state().state == "stopped"))
        self.compute.pousse(etat("draining", 2))
        self.assertTrue(attendre(lambda: g.wait_change(0.2).state == "draining"))
        # coupure : l'état tient `lost_grace`, puis vaut le repli
        self.compute.coupe()
        self.assertTrue(attendre(lambda: g.state().state == "stopped", timeout=3.0))
        # le runner revient : reconnexion et ack en attente renvoyé
        g.acknowledge(P.GateAck(seq=2, state="draining", drained=True))
        self.compute.accepte()
        self.compute.pousse(etat("available", 3))
        self.assertTrue(attendre(lambda: g.state().seq == 3))
        self.assertTrue(attendre(lambda: any(a.get("drained") for a in self.compute.acks)))


# ==========================================================================
# 2. contrôleur
# ==========================================================================

class _FausseGate(P.HostGate):
    def __init__(self, state):
        self.st = state
        self.acks: list[P.GateAck] = []

    def state(self):
        return self.st

    def wait_change(self, timeout):
        return self.st

    def acknowledge(self, ack):
        self.acks.append(ack)


class _FauxWorker:
    def __init__(self, name, en_tour=True):
        self.name = name
        self.proc = object() if en_tour else None
        self.host_yield = None
        self.wake = threading.Event()
        self.vivant = True
        self.retraits: list[str] = []

    def is_alive(self):
        return self.vivant

    def yield_to_host(self, raison):
        self.host_yield = raison
        self.retraits.append(raison)


class _FauxRunner:
    def __init__(self):
        self.lock = threading.Lock()
        self.workers: dict = {}
        self.wake_all = threading.Event()


class ControleurTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.gate = _FausseGate(P.GateState("available", 1, reason="idle"))
        self.runner = _FauxRunner()
        self.sink = mock.Mock()
        self.log: list[str] = []
        self.ctl = H.GateController(self.runner, self.gate, drain_s=90.0, sink=self.sink,
                                    log=self.log.append, clock=lambda: self.now)
        self.ctl.apply(self.gate.st)

    def test_transitions_et_acquittements(self):
        a = _FauxWorker("a")
        self.runner.workers["a"] = a
        self.assertTrue(self.ctl.may_claim())
        self.assertFalse(self.ctl.tick().drained)
        # available -> draining : plus de réclamation, échéance par défaut
        self.ctl.apply(P.GateState("draining", 2, reason="user_active"))
        self.assertTrue(self.ctl.holds_turns())
        self.assertEqual(self.ctl.deadline, 1090.0)
        self.assertTrue(a.wake.is_set())
        ack = self.ctl.tick()
        self.assertEqual((ack.seq, ack.in_turn, ack.held, ack.drained),
                         (2, ("a",), ("a",), False))
        self.assertEqual(a.retraits, [])          # avant l'échéance : point sûr
        self.now = 1090.0
        self.ctl.tick()
        self.assertEqual(a.retraits, ["échéance du retrait de l'hôte"])
        self.ctl.tick()
        self.assertEqual(len(a.retraits), 1)      # une seule fois par tour
        a.vivant = False                          # bail rendu
        ack = self.ctl.tick()
        self.assertTrue(ack.drained)
        self.assertEqual(self.gate.acks[-1], ack)
        n = len(self.gate.acks)
        self.ctl.tick()
        self.assertEqual(len(self.gate.acks), n)  # inchangé : pas de nouvel ack
        # draining -> available : reprise
        self.runner.workers.clear()
        self.ctl.apply(P.GateState("available", 3, reason="idle"))
        self.assertTrue(self.runner.wake_all.is_set())
        self.assertIsNone(self.ctl.deadline)
        self.assertFalse(self.ctl.tick().drained)
        # rapports au serveur : un par état, et les acquittements
        etats = [c.args[0].state for c in self.sink.report.call_args_list]
        self.assertIn("draining", etats)
        self.assertEqual(etats[-1], "available")

    def test_echeance_du_runner_et_jamais_repoussee(self):
        self.ctl.apply(P.GateState("draining", 2, drain_deadline_ts=1010.0))
        self.assertEqual(self.ctl.deadline, 1010.0)
        self.ctl.apply(P.GateState("draining", 3, drain_deadline_ts=2000.0))
        self.assertEqual(self.ctl.deadline, 1010.0)
        self.now = 1005.0
        self.ctl.apply(P.GateState("stopped", 4))
        self.assertEqual(self.ctl.deadline, 1005.0)

    def test_stopped_arrete_tout_de_suite(self):
        a, b = _FauxWorker("a"), _FauxWorker("b", en_tour=False)
        self.runner.workers.update(a=a, b=b)
        self.ctl.apply(P.GateState("stopped", 2, reason="battery"))
        ack = self.ctl.tick()
        self.assertEqual(a.retraits, ["hôte arrêté"])
        self.assertEqual(b.retraits, [])
        self.assertEqual(ack.held, ("a", "b"))
        self.assertTrue(b.wake.is_set())

    def test_retard_journalise(self):
        a = _FauxWorker("a")
        self.runner.workers["a"] = a
        self.ctl.apply(P.GateState("draining", 2))
        self.now = 1090.0 + H.OVERDUE_GRACE_S
        self.ctl.tick()
        self.assertTrue(any("en retard" in m for m in self.log))

    def test_plafond_de_concurrence(self):
        # contrat 1.1 : sans `caps.max_concurrent`, le défaut explicite
        self.assertEqual(self.ctl.max_concurrent(), P.DEFAULT_MAX_CONCURRENT)
        self.ctl.apply(P.GateState("available", 2, caps={"max_concurrent": 3}))
        self.assertEqual(self.ctl.max_concurrent(), 3)


# ==========================================================================
# 3. configuration, bail, serveur
# ==========================================================================

class ConfigTest(unittest.TestCase):
    def charge(self, **env):
        base = child_env(AMEESH_CONFIG="/nonexistent/config.json")
        base.update(env)
        return config_mod.load(env=base)

    def test_defauts_et_reglages(self):
        cfg = self.charge()
        self.assertEqual((cfg.host_gate, cfg.host_mediated, cfg.host_gate_drain), ("", False, 90.0))
        self.assertIsNone(H.from_config(cfg))  # hôte classique : aucune porte
        cfg = self.charge(AMEESH_HOST_GATE="file", AMEESH_HOST_GATE_PATH="/x/state.json",
                          AMEESH_HOST_MEDIATED="1", AMEESH_HOST_GATE_DRAIN="30")
        self.assertEqual((cfg.host_gate, cfg.host_gate_path, cfg.host_gate_drain),
                         ("file", "/x/state.json", 30.0))
        self.assertEqual(H.default_fallback(cfg), "stopped")
        g = H.from_config(cfg)
        self.assertIsInstance(g, H.FileGate)
        self.assertEqual(g.ack_path, "/x/ack.json")
        g.close()
        self.assertEqual(H.default_fallback(dataclasses.replace(cfg, host_mediated=False)),
                         "available")
        self.assertEqual(H.default_fallback(dataclasses.replace(
            cfg, host_mediated=False, dsn="https://mesh.example/api")), "stopped")
        for mauvais in ({"AMEESH_HOST_GATE": "pigeon"},
                        {"AMEESH_HOST_GATE_FALLBACK": "peut-être"}):
            with self.assertRaises(SystemExit):
                self.charge(**mauvais)


class BailVolatilTest(_Canons, unittest.TestCase):
    def charge(self, extra: str):
        self.n = getattr(self, "n", 0) + 1
        return canon.load(self.manaty(name="c%d" % self.n,
                                      host_text=host("alice", resources_yaml=extra)))

    def test_bail_de_la_fiche(self):
        self.assertEqual(H.lease_ttl_for([self.charge("")], HOST, 300.0), 300.0)
        c = self.charge("  volatile: true\n")
        self.assertTrue(c.host(HOST).policy.volatile)
        self.assertEqual(H.lease_ttl_for([c], HOST, 300.0), 90.0)
        c = self.charge("  volatile: true\n  lease_ttl: 45\n")
        self.assertEqual(H.lease_ttl_for([c], HOST, 300.0), 45.0)
        self.assertEqual(H.lease_ttl_for([c], "autre-hote", 300.0), 300.0)

    def test_fiche_invalide(self):
        c = self.charge("  volatile: peut-etre\n  lease_ttl: 2\n")
        codes = [f.code for f in c.load_findings]
        self.assertGreaterEqual(codes.count("host-policy-invalid"), 2)
        self.assertTrue(c.host(HOST).policy.volatile)  # prudence : bail court


class DisponibiliteTest(unittest.TestCase):
    def setUp(self):
        with open(GOLDEN, encoding="utf-8") as fh:
            self.golden = json.load(fh)

    def test_corps_dore_et_admission(self):
        body = self.golden["disponibilite"]["requete"]["corps"]
        self.assertEqual(D.check_body(body), body)
        for mauvais in (dict(body, schema="x"), dict(body, state="sleeping"),
                        dict(body, available=True), dict(body, seq="8")):
            with self.assertRaises(ValueError):
                D.check_body(mauvais)
        reg = D.MemoryRegistry()
        self.assertTrue(D.admissible(reg.get("pc")))  # hôte inconnu : comme avant
        reg.put("pc", body, now=10.0)
        self.assertFalse(D.admissible(reg.get("pc")))
        reg.put("pc", P.availability_body(P.GateState("available", 9)), now=20.0)
        self.assertTrue(D.admissible(reg.get("pc")))

    def test_registre_fichier_et_since(self):
        path = os.path.join(tempfile.mkdtemp(prefix="ameesh-l112-r-"), "exec", "a.json")
        reg = D.FileRegistry(path)
        drain = P.availability_body(P.GateState("draining", 2))
        reg.put("pc", drain, now=100.0)
        reg.put("pc", dict(drain, seq=3), now=150.0)
        row = reg.get("pc")
        self.assertEqual((row["since_ts"], row["updated_ts"]), (100.0, 150.0))
        reg.put("pc", P.availability_body(P.GateState("available", 4)), now=200.0)
        self.assertEqual(reg.get("pc")["since_ts"], 200.0)
        self.assertEqual(D.FileRegistry(path + ".absent").rows(), [])

    def test_alertes(self):
        reg = D.MemoryRegistry()
        reg.put("illisible", P.availability_body(P.GateState("stopped", -1, reason="unknown")),
                now=1000.0)
        reg.put("pret", P.availability_body(P.GateState("stopped", 3, reason="user_active")),
                now=1000.0)
        reg.put("draine", P.availability_body(P.GateState("draining", 5, reason="user_active")),
                now=1000.0,
                gate=P.GateState("draining", 5, drain_deadline_ts=1010.0).to_json(),
                ack=P.GateAck(5, "draining", held=("x",)).to_json())
        out = D.alerts(reg.rows(), now=1050.0)
        self.assertEqual(sorted((a["type"], a["host"]) for a in out),
                         [("host_drain_overdue", "draine"), ("host_unavailable", "illisible")])
        # la base fait foi : plus aucun bail détenu, pas de retard
        out = D.alerts(reg.rows(), now=1050.0, held={})
        self.assertEqual([a["type"] for a in out], ["host_unavailable"])
        # une longue absence alerte ; le type part par défaut avec notify
        out = D.alerts(reg.rows(), now=1000.0 + D.DEFAULT_UNAVAILABLE_ALERT_S, held={})
        self.assertIn(("host_unavailable", "pret"), [(a["type"], a["host"]) for a in out])
        for kind in D.ALERT_TYPES:
            self.assertIn(kind, exploitation.ALERT_TYPES)
            self.assertIn(kind, notify.DEFAULT_TYPES)
            self.assertIn(kind, notify.TYPE_LABELS)
        msg = notify.render(out[0], "raised", 1000.0 + D.DEFAULT_UNAVAILABLE_ALERT_S)
        self.assertIn("hôte", msg.title)


# ==========================================================================
# 4. l'exécuteur réel
# ==========================================================================

#: faux harnais « claude » : appels d'outils (tool_use, attente, tool_result)
FAUX_HARNAIS = r'''#!/usr/bin/env python3
import json, os, sys, time
journal = os.environ["L112_JOURNAL"]
def note(m):
    with open(journal, "a") as fh:
        fh.write(json.dumps(m) + "\n")
args = sys.argv[1:]
session = args[args.index("--resume") + 1] if "--resume" in args else "sess-l112"
note({"debut": True, "resume": "--resume" in args})
print(json.dumps({"type": "system", "subtype": "init", "session_id": session}), flush=True)
for i in range(int(os.environ.get("L112_ETAPES", "3"))):
    print(json.dumps({"type": "assistant", "message": {"id": "m%d" % i, "content": [
        {"type": "tool_use", "id": "t%d" % i, "name": "Bash", "input": {}}]}}), flush=True)
    note({"outil": i})
    time.sleep(float(os.environ.get("L112_DUREE", "0.6")))
    print(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t%d" % i, "content": "ok"}]}}), flush=True)
    note({"fini": i})
print(json.dumps({"type": "result", "subtype": "success", "result": "fini",
                  "total_cost_usd": 0.0, "session_id": session}), flush=True)
note({"fin": True})
'''


class ExecuteurTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.gate_dir = os.path.join(self.tmp, "gate")
        os.makedirs(self.gate_dir)
        self.state_path = os.path.join(self.gate_dir, "state.json")
        self.ack_path = os.path.join(self.gate_dir, "ack.json")
        self.journal = os.path.join(self.tmp, "harnais.jsonl")
        script = os.path.join(self.tmp, "claude-l112")
        with open(script, "w") as fh:
            fh.write(FAUX_HARNAIS)
        os.chmod(script, 0o755)
        env = mock.patch.dict(os.environ, {"AMEESH_CLAUDE_BIN": script,
                                           "L112_JOURNAL": self.journal})
        env.start()
        self.addCleanup(env.stop)

    def harnais(self) -> list[dict]:
        if not os.path.exists(self.journal):
            return []
        with open(self.journal) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def ack(self) -> dict | None:
        try:
            with open(self.ack_path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def agent(self, name="enfant", prompt="travail"):
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd, prompt=prompt)

    def base(self):
        """Exécuteur en processus : aucun canon du poste (pas de `canon sync`)."""
        return dataclasses.replace(self.cfg, budget_usd_per_hour=0.0, poll=0.3, canon="",
                                   canon_ref="", extra_canons=(), require_responsible=False)

    def base_db(self, conf):
        """Connexion de l'exécuteur, sous SA configuration (R14 : sans canon,
        pas de responsable exigé)."""
        db = db_mod.connect(conf)
        self.addCleanup(db.close)
        return db

    def lance(self, *, mediated=True, gate="file", **cfg) -> Runner:
        conf = dataclasses.replace(self.base(), host_gate=gate, host_gate_path=self.state_path,
                                   host_mediated=mediated, **cfg)
        runner = Runner(conf, self.base_db(conf))
        fil = threading.Thread(target=runner.run, daemon=True)
        fil.start()

        def arret():
            runner.stop.set()
            runner.wake_all.set()
            fil.join(timeout=20)
        self.addCleanup(arret)
        return runner

    def test_retrait_pendant_un_tour_au_point_sur_puis_reprise(self):
        self.agent()
        ecrit_etat(self.state_path, etat("available", 1))
        os.environ["L112_ETAPES"] = "6"
        self.addCleanup(os.environ.pop, "L112_ETAPES", None)
        self.lance()
        self.assertTrue(attendre(lambda: any("outil" in m for m in self.harnais())))
        finis_avant = sum(1 for m in self.harnais() if "fini" in m)
        ecrit_etat(self.state_path, etat("draining", 2,
                                         drain_deadline_ts=time.time() + 60))
        ack = attendre(lambda: (self.ack() or {}).get("drained") and self.ack())
        self.assertTrue(ack, "le retrait doit finir par un acquittement drained")
        self.assertEqual((ack["seq"], ack["state"], ack["held"]), (2, "draining", []))
        vus = self.harnais()
        self.assertFalse(any(m.get("fin") for m in vus), "tour arrêté avant sa fin")
        # l'appel d'outil en cours a fini (point sûr), aucun autre après lui
        self.assertIn(sum(1 for m in vus if "fini" in m), (finis_avant, finis_avant + 1))
        self.assertGreaterEqual(sum(1 for m in vus if "fini" in m), 1)
        row = registry.get(self.db, "enfant")
        self.assertEqual(row["pending_prompt"], "travail")
        self.assertIsNone(row["lease_owner"])
        self.assertIn("hôte indisponible", row["status_text"])
        with open(os.path.join(self.cfg.agent_dir("enfant"), "session")) as fh:
            self.assertEqual(fh.read().strip(), "sess-l112")  # session gardée
        # rien n'est réclamé tant que l'hôte n'est pas revenu
        time.sleep(1.0)
        self.assertIsNone(registry.get(self.db, "enfant")["lease_owner"])
        # retour à available : reprise sans geste humain, sur la même session
        os.environ["L112_ETAPES"] = "1"
        ecrit_etat(self.state_path, etat("available", 3))
        self.assertTrue(attendre(lambda: any(m.get("fin") for m in self.harnais())),
                        "le tour doit reprendre de lui-même")
        reprise = [m for m in self.harnais() if m.get("debut")][-1]
        self.assertTrue(reprise["resume"])
        self.assertTrue(attendre(
            lambda: registry.get(self.db, "enfant")["pending_prompt"] is None))

    def test_echeance_depassee(self):
        self.agent()
        ecrit_etat(self.state_path, etat("available", 1))
        os.environ.update(L112_ETAPES="1", L112_DUREE="60")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("L112_ETAPES",
                                                                   "L112_DUREE")])
        self.lance()
        self.assertTrue(attendre(lambda: any("outil" in m for m in self.harnais())))
        debut = time.monotonic()
        ecrit_etat(self.state_path, etat("draining", 2, drain_deadline_ts=time.time() + 1.0))
        ack = attendre(lambda: (self.ack() or {}).get("drained") and self.ack())
        self.assertTrue(ack)
        self.assertLess(time.monotonic() - debut, 12.0)
        self.assertGreaterEqual(time.monotonic() - debut, 0.9)
        self.assertFalse(any("fini" in m for m in self.harnais()))
        row = registry.get(self.db, "enfant")
        self.assertEqual(row["pending_prompt"], "travail")
        self.assertIsNone(row["lease_owner"])

    def test_stopped_aucun_tour(self):
        self.agent()
        ecrit_etat(self.state_path, etat("available", 1))
        os.environ.update(L112_ETAPES="1", L112_DUREE="60")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("L112_ETAPES",
                                                                   "L112_DUREE")])
        self.lance()
        self.assertTrue(attendre(lambda: any("outil" in m for m in self.harnais())))
        debut = time.monotonic()
        ecrit_etat(self.state_path, etat("stopped", 2, reason="revoked"))
        ack = attendre(lambda: (self.ack() or {}).get("drained") and self.ack())
        self.assertTrue(ack)
        self.assertEqual(ack["state"], "stopped")
        self.assertLess(time.monotonic() - debut, 8.0)
        self.assertEqual(registry.get(self.db, "enfant")["pending_prompt"], "travail")
        n = len(self.harnais())
        time.sleep(1.0)
        self.assertEqual(len(self.harnais()), n, "aucun tour pendant stopped")

    def test_etat_illisible_hote_medie(self):
        self.agent()
        ecrit_etat(self.state_path, "{illisible")
        runner = self.lance()
        ack = attendre(self.ack)
        self.assertEqual((ack["state"], ack["drained"]), ("stopped", True))
        time.sleep(1.0)
        self.assertEqual(self.harnais(), [])
        self.assertIsNone(registry.get(self.db, "enfant")["lease_owner"])
        self.assertTrue(runner.gate_holds())
        # l'état redevient lisible : reprise
        os.environ["L112_ETAPES"] = "1"
        self.addCleanup(os.environ.pop, "L112_ETAPES", None)
        ecrit_etat(self.state_path, etat("available", 2))
        self.assertTrue(attendre(lambda: any(m.get("fin") for m in self.harnais())))

    def test_etat_absent_hote_classique_avec_porte(self):
        self.agent()
        os.environ["L112_ETAPES"] = "1"
        self.addCleanup(os.environ.pop, "L112_ETAPES", None)
        runner = self.lance(mediated=False)  # aucun fichier d'état
        self.assertFalse(runner.gate_holds())
        self.assertTrue(attendre(lambda: any(m.get("fin") for m in self.harnais())))
        # registre local de disponibilité : la porte y est dite
        rows = D.FileRegistry(D.default_path(self.cfg)).rows()
        self.assertEqual([(r["host"], r["body"]["state"]) for r in rows],
                         [(self.cfg.host, "available")])

    def test_hote_classique_inchange(self):
        self.agent()
        os.environ["L112_ETAPES"] = "2"
        self.addCleanup(os.environ.pop, "L112_ETAPES", None)
        runner = Runner(self.base(), self.base_db(self.base()))
        self.assertIsNone(runner.host_gate)
        self.assertFalse(runner.gate_holds())
        fil = threading.Thread(target=runner.run, daemon=True)
        fil.start()
        self.addCleanup(lambda: (runner.stop.set(), runner.wake_all.set(),
                                 fil.join(timeout=20)))
        self.assertTrue(attendre(lambda: any(m.get("fin") for m in self.harnais())))
        # le tour va à son terme, points sûrs compris
        self.assertEqual(sum(1 for m in self.harnais() if "fini" in m), 2)
        self.assertFalse(os.path.exists(self.ack_path))
        self.assertFalse(os.path.exists(D.default_path(self.cfg)))
        self.assertTrue(attendre(
            lambda: registry.get(self.db, "enfant")["pending_prompt"] is None))

    def test_plafond_de_concurrence_de_la_porte(self):
        self.agent("un")
        self.agent("deux")
        os.environ.update(L112_ETAPES="1", L112_DUREE="2")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("L112_ETAPES",
                                                                   "L112_DUREE")])
        ecrit_etat(self.state_path, etat("available", 1, caps={"max_concurrent": 1}))
        runner = self.lance()
        self.assertTrue(attendre(lambda: runner.workers))
        time.sleep(0.8)
        with runner.lock:
            self.assertEqual(len([w for w in runner.workers.values() if w.is_alive()]), 1)

    def test_alertes_du_poste(self):
        self.agent()
        ecrit_etat(self.state_path, "{illisible")
        self.lance(mediated=False, host_gate_fallback="stopped")
        self.assertTrue(attendre(lambda: os.path.exists(D.default_path(self.cfg))))
        out = attendre(lambda: [a for a in exploitation.alerts(self.cfg, self.db)
                                if a["type"] == "host_unavailable"])
        self.assertTrue(out)
        self.assertEqual(out[0]["host"], self.cfg.host)
        self.assertIn("porte illisible", out[0]["detail"])


if __name__ == "__main__":
    unittest.main()
