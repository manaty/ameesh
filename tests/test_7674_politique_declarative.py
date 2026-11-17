# SPDX-License-Identifier: AGPL-3.0-only
"""#7674 (contrat public #7636 / E3a) : politique de session déclarative.

Une carte Agent/Persona déclare `session_policy`, `fresh_session`,
`context_max_tokens` et `failure_wait_min` ; `canon sync` la porte au registre
et l'exécuteur la consomme, sans sous-classe de `runner.AgentWorker`. Les
témoins couvrent la fiche, la synchronisation, le document et l'agent ordinaire
côte à côte, l'argument optionnel `harness`, le délai après échec et l'arrêt
propre pendant l'attente.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from ameesh import canon, canon_sync, exploitation, registry, runner
from ameesh.config import Config

from . import test_canon as canon_tests


def _card(**fields) -> str:
    """Une fiche Agent minimale avec les champs demandés dans le frontmatter."""
    lines = ["---", "type: Agent", "title: doc", "responsible: human:alice",
             "team: acme", "capabilities: [read]"]
    for key, value in fields.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        lines.append("%s: %s" % (key, value))
    lines += ["---", "", "# doc", ""]
    return "\n".join(lines)


class DeclaredPolicyCardTest(unittest.TestCase):
    """La fiche déclare, et le validateur refuse ce qui est illisible."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="test-7674-card-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "agents"))
        with open(os.path.join(self.root, "index.md"), "w", encoding="utf-8") as fh:
            fh.write("---\ntype: Index\ntitle: x\n---\n")

    def load(self) -> canon.Canon:
        return canon.load(self.root, untrusted=True)

    def test_politique_lue_et_serialisee(self):
        canon_tests.write(self.root, "agents/doc.md", _card(
            session_policy="jamais", fresh_session="true",
            context_max_tokens="15M", failure_wait_min="60s"))
        agent = self.load().agent("doc")
        self.assertIsNotNone(agent)
        self.assertEqual(agent.declared_policy(), {
            "session_policy": "jamais", "fresh_session": True,
            "context_max_tokens": 15_000_000, "failure_wait_min": 60})
        # rien de déclaré : None, jamais un objet vide qui écraserait un réglage
        canon_tests.write(self.root, "agents/doc.md", _card())
        self.assertIsNone(self.load().agent("doc").declared_policy())

    def test_valeurs_illisibles_sont_des_erreurs(self):
        canon_tests.write(self.root, "agents/doc.md", _card(
            session_policy="toujours", fresh_session="peut-etre",
            context_max_tokens=-1, failure_wait_min="-5s"))
        findings = canon.validate(self.load())
        codes = {f.code for f in findings if f.severity == canon.ERROR}
        self.assertTrue({
            "agent-session-policy-invalid", "agent-fresh-session-invalid",
            "agent-context-max-invalid", "agent-failure-wait-invalid",
        } <= codes, sorted(codes))

    def test_session_neuve_et_rotation_sont_contradictoires(self):
        canon_tests.write(self.root, "agents/doc.md", _card(
            fresh_session="true", session_policy="taille"))
        findings = canon.validate(self.load())
        self.assertIn("agent-session-policy-conflict",
                      {f.code for f in findings if f.severity == canon.ERROR})


class DeclaredPolicySyncTest(canon_tests._CanonDbCase):
    """`canon sync` écrit la politique déclarée, et l'efface quand elle part."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.root, "agents", "orchestre.md"),
                  encoding="utf-8") as fh:
            self.original_agent = fh.read()

    def set_policy(self, **fields) -> None:
        path = os.path.join(self.root, "agents", "orchestre.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        body = []
        for key, value in fields.items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            body.append("%s: %s" % (key, value))
        text = text.replace("\n---\n\n# orchestre",
                            "\n%s\n---\n\n# orchestre" % "\n".join(body), 1)
        canon_tests.write(self.root, "agents/orchestre.md", text)

    def test_sync_porte_puis_efface_la_politique(self):
        self.set_policy(session_policy="jamais", fresh_session="true",
                        context_max_tokens="15M", failure_wait_min="60s")
        report = self.sync()
        self.assertEqual(report.status, canon_sync.CANON_OK, report.diagnostic)
        row = self.row("orchestre")
        self.assertEqual(row["declared_policy"], {
            "session_policy": "jamais", "fresh_session": True,
            "context_max_tokens": 15_000_000, "failure_wait_min": 60})
        # la vue d'exploitation expose la politique et l'effectif qui en découle
        view = exploitation.agent_view(self.cfg, row)
        self.assertEqual(view["declared_policy"], row["declared_policy"])
        self.assertEqual(view["session_policy"], "jamais")
        self.assertEqual(view["context_max_tokens"], 15_000_000)
        # `ameesh list --json` part de `operations.listing()` : la politique
        # doit y être lue aussi, pas seulement sur la ligne du registre
        rows = [{"name": "orchestre"}]
        exploitation.annotate(self.cfg, self.db, rows)
        self.assertEqual(rows[0]["declared_policy"], row["declared_policy"])
        self.assertEqual(rows[0]["session_policy"], "jamais")
        # une synchronisation suivante, fiche inchangée : rien à réécrire
        self.assertEqual(self.actions(self.sync())["orchestre"], "inchangé")
        # la fiche ne déclare plus rien : le registre revient à NULL
        canon_tests.write(self.root, "agents/orchestre.md", self.original_agent)
        self.assertEqual(self.actions(self.sync())["orchestre"], "mis à jour")
        self.assertIsNone(self.row("orchestre")["declared_policy"])

    def test_valeur_illisible_ferme_la_reclamation(self):
        self.set_policy(session_policy="toujours")
        report = self.sync()
        # une erreur propre à l'agent ne ferme pas le canon de l'hôte, mais
        # l'agent n'est plus réclamable (fail closed)
        self.assertEqual(report.status, canon_sync.CANON_OK, report.diagnostic)
        self.assertIn("agent-session-policy-invalid",
                      {f.code for f in report.findings})
        self.assertNotIn("orchestre",
                         [row["name"] for row in registry.claimable(
                             self.db, "atelier", require_responsible=True)])

    def test_lecture_n_1_sans_la_colonne(self):
        # Un écrivain N-1 ne nomme pas la colonne ajoutée : la lecture neuve
        # doit tenir (NULL = aucune politique déclarée) et retomber sur les
        # réglages impératifs et les défauts.
        self.previous_insert("agent_registry",
                             {"name": "n1-7674", "harness": "claude", "host": "atelier"},
                             added=["declared_policy"])
        row = registry.get(self.db, "n1-7674")
        self.assertIsNone(row["declared_policy"])
        self.assertEqual(exploitation.effective_policy(self.cfg, row), "par-lot")
        self.assertEqual(exploitation.declared_failure_wait_min(row), 0.0)


def _worker(declared: dict | None = None, *, name: str = "doc") -> runner.AgentWorker:
    """Un worker minimal : session, contexte et attente sans base ni harnais."""
    worker = runner.AgentWorker.__new__(runner.AgentWorker)
    threading.Thread.__init__(worker, name="test-7674-" + name, daemon=True)
    worker.name = name
    worker.cfg = dataclasses.replace(
        Config(), context_max_tokens_by_harness={"deepseek": 250_000, "codex": 15_000_000})
    worker.agent = {"name": name, "harness": "deepseek",
                    "session_id": "sess-%s" % name, "session_reset_ts": None,
                    "declared_policy": declared}
    worker.state_dir = tempfile.mkdtemp(prefix="test-7674-runner-")
    worker.proc = None
    worker.session_turns = 3
    worker.session_tokens = 0
    worker.last_turn_seconds = 0
    worker.fast_failures = 0
    worker.failure_streak = 0
    worker.rate_limit = None
    worker._move_turn = None
    worker.runner = SimpleNamespace(failure_backoff=5.0, failure_backoff_max=300.0,
                                    session_min_turns=3, session_max_tokens=150_000,
                                    session_max_turn_seconds=900.0,
                                    stop=threading.Event(), draining=threading.Event(),
                                    gate_holds=lambda: False)
    return worker


class DeclaredPolicyRunnerTest(unittest.TestCase):
    """Le document et l'agent ordinaire côte à côte, sur la même configuration."""

    def test_session_neuve_et_reprise(self):
        document = _worker({"fresh_session": True, "session_policy": "jamais"})
        ordinary = _worker(None, name="ordinaire")
        try:
            self.assertIsNone(document.current_session())
            self.assertEqual(ordinary.current_session(), "sess-ordinaire")
            self.assertEqual(document.session_policy(), "jamais")
            self.assertEqual(ordinary.session_policy(), "par-lot")
        finally:
            shutil.rmtree(document.state_dir, ignore_errors=True)
            shutil.rmtree(ordinary.state_dir, ignore_errors=True)

    def test_contexte_declare_quel_que_soit_le_harnais(self):
        document = _worker({"context_max_tokens": 0})
        ordinary = _worker(None, name="ordinaire")
        try:
            # `0` déclaré est opposable au défaut `deepseek` (250 k), avec ou
            # sans l'argument optionnel `harness`.
            self.assertEqual(document.context_max_tokens(), 0)
            self.assertEqual(document.context_max_tokens("deepseek"), 0)
            self.assertEqual(document.context_max_tokens("claude"), 0)
            self.assertEqual(ordinary.context_max_tokens(), 250_000)
            self.assertEqual(ordinary.context_max_tokens("codex"), 15_000_000)
        finally:
            shutil.rmtree(document.state_dir, ignore_errors=True)
            shutil.rmtree(ordinary.state_dir, ignore_errors=True)

    def test_attente_bornee_par_le_plancher_declare(self):
        document = _worker({"failure_wait_min": 60})
        ordinary = _worker(None, name="ordinaire")
        try:
            self.assertEqual(ordinary.failure_wait(), 5.0)
            # sous le plancher : 60 s ; au-delà, le plafond de l'exécuteur
            self.assertEqual(document.failure_wait(), 60.0)
            document.failure_streak = 8
            self.assertEqual(document.failure_wait(), 300.0)
            # un plancher déclaré plus haut que le plafond ne le perce pas
            document.agent["declared_policy"] = {"failure_wait_min": 600}
            self.assertEqual(document.failure_wait(), 300.0)
            # l'échéance durable d'un débit/quota reste seule juge
            document.rate_limit = "quota"
            document.retry_remaining = lambda: 7.0
            self.assertEqual(document.failure_wait(), 7.0)
        finally:
            shutil.rmtree(document.state_dir, ignore_errors=True)
            shutil.rmtree(ordinary.state_dir, ignore_errors=True)

    def test_arret_propre_pendant_l_attente(self):
        document = _worker({"failure_wait_min": 60})
        document.wake = threading.Event()

        def renew_wait(rest: float) -> float:
            return min(max(rest, 0.01), 0.05)

        document.renew_wait = renew_wait
        document.renew_if_due = lambda: True
        document.stopping = threading.Event()
        wait = document.failure_wait()
        thread = threading.Thread(target=document._attendre_apres_echec, args=(wait,))
        thread.start()
        time.sleep(0.1)
        document.stopping.set()
        thread.join(2.0)
        try:
            self.assertFalse(thread.is_alive())
        finally:
            shutil.rmtree(document.state_dir, ignore_errors=True)
