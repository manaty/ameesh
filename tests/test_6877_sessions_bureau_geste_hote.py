# SPDX-License-Identifier: AGPL-3.0-only
"""#6877 : deux sessions du bureau (BG et PC) partagent l'identité et la clé
SSH de BG ; « exécutant unique » sans garde mécanique.

Le constat du 11/11 sur manaty-its-node : apply-services et un lancement de
services ont été faits depuis l'IP de BG avec sa clé générale, sans qu'aucune
session ne les revendique ; sshd ne distingue pas les deux sessions. Le lot
ajoute deux pièces mécaniques, sans toucher aux clés (décision owner) :

* le fil nomme la session VÉRIFIÉE qui écrit (L41, PID ancêtre), au lieu de
  la seule identité « bureau » ;
* `ameesh work claim <lot> --check` est la porte, en lecture seule, qu'un
  runbook appelle avant son premier geste hôte : elle n'accepte que la
  session liée qui tient la prise, et refuse une seconde session.

Les clés SSH séparées par session restent une proposition owner (aucune
modification de clé dans ce lot)."""
from __future__ import annotations

import json
import os

from ameesh import fil, work

from . import test_3447_prise_externe as prise_externe
from .test_l119_backlog import _Base


class SessionDansLeFilTest(_Base):
    """La session liée qui écrit est distinguable dans le fil (#6877)."""

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM session_bindings")
        self.addCleanup(lambda: self.db.execute("DELETE FROM session_bindings"))

    def unbound_env(self, **extra):
        env = self.env(**extra)
        env.pop("AGENT_MAIL_NAME", None)
        return env

    def external_agent(self, name="bureau"):
        self.agent(name, harness="claude")
        self.db.execute("UPDATE agent_registry SET mode = 'externe',"
                        " lease_owner = NULL, lease_expires_at = NULL,"
                        " status = 'idle' WHERE name = %s", (name,))

    def bind(self, session="s-bg", *, agent="bureau"):
        self.db.execute(
            "INSERT INTO session_bindings"
            " (session_id, harness, host, agent, pid, created_by)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (session, "claude", self.cfg.host, agent, os.getpid(), "test"))

    def entry(self):
        """La dernière entrée écrite dans un fil, ses métadonnées comprises."""
        [row] = self.db.query(
            "SELECT project, lot FROM thread_index ORDER BY last_at DESC LIMIT 1")
        transport = fil.transport_for(self.cfg)
        entries = transport.read(fil.ThreadRef(row["project"], row["lot"] or None))
        return entries[-1]

    def test_une_session_liee_signe_son_entree_dans_le_fil(self):
        self.external_agent()
        self.agent("alpha", harness="codex")
        self.bind("s-bg")
        proc = self.cli("send", "alpha", "Message de la session BG.",
                        env=self.unbound_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self.entry()
        self.assertIn("Message de la session BG.", entry.text)
        self.assertEqual(entry.meta.get("session"), "s-bg")
        self.assertEqual(entry.meta.get("harness"), "claude")

    def test_une_liaison_verifiee_prime_sur_un_nom_annonce(self):
        """Le cas du bureau : `AGENT_MAIL_NAME` encore posé dans le shell ne
        doit pas masquer la liaison de session (L77, #4485)."""
        self.external_agent()
        self.agent("alpha", harness="codex")
        self.bind("s-bg")
        proc = self.cli("send", "alpha", "Message avec nom annoncé.",
                        env=self.env(AGENT_MAIL_NAME="bureau"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self.entry()
        self.assertEqual(entry.meta.get("session"), "s-bg")
        self.assertEqual(entry.meta.get("harness"), "claude")

    def test_sans_liaison_le_fil_ne_nomme_aucune_session(self):
        self.external_agent()
        self.agent("alpha", harness="codex")
        proc = self.cli("send", "alpha", "Message sans liaison.",
                        "--from", "bureau", env=self.unbound_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self.entry()
        self.assertNotIn("session", entry.meta)
        self.assertNotIn("harness", entry.meta)

    def test_une_liaison_sans_pid_ne_signe_pas_le_fil(self):
        """Une liaison sans PID ne prouve pas la session : elle ne doit pas
        être annoncée comme vérifiée dans le fil."""
        self.external_agent()
        self.agent("alpha", harness="codex")
        self.db.execute(
            "INSERT INTO session_bindings"
            " (session_id, harness, host, agent, pid, created_by)"
            " VALUES ('s-sans-pid', 'claude', %s, 'bureau', NULL, 'test')",
            (self.cfg.host,))
        proc = self.cli("send", "alpha", "Message d'une liaison sans PID.",
                        "--from", "bureau", env=self.unbound_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self.entry()
        self.assertNotIn("session", entry.meta)


class GesteHoteGateTest(_Base):
    """`work claim --check` : la porte avant un geste hôte (#6877)."""

    external_agent = prise_externe.PriseExterneTest.external_agent

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM session_bindings")
        self.addCleanup(lambda: self.db.execute("DELETE FROM session_bindings"))

    def reserve(self):
        self.external_agent()
        lot = work.add(self.db, title="geste hôte ITS", app="nexlink",
                       actor="human:proprio", cfg=self.cfg,
                       external_executor="bureau")
        return work.get(self.db, lot["id"])

    def bind_session(self, session_id, *, agent="bureau"):
        # une session externe ne se lie pas à un agent mené par un bail (L41)
        self.db.execute("UPDATE agent_registry SET lease_owner = NULL,"
                        " lease_expires_at = NULL, status = 'idle'"
                        " WHERE name = %s", (agent,))
        self.db.execute(
            "INSERT INTO session_bindings"
            " (session_id, harness, host, agent, pid, created_by)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (session_id, "claude", self.cfg.host, agent, os.getpid(), "test"))

    def claim(self, lot_id, session, *, proof=None):
        return work.claim_external(self.db, lot_id, session, actor="bureau",
                                   proof=proof if proof is not None else session,
                                   cfg=self.cfg)

    def check(self, lot_id, session, *, proof=None):
        return work.check_external_claim(self.db, lot_id, session, proof=proof)

    def test_la_porte_refuse_sans_prise_sans_preuve_et_pour_une_autre_session(self):
        lot = self.reserve()
        with self.assertRaises(work.WorkError) as raised:
            self.check(lot["id"], "BG", proof=None)
        self.assertIn("verified session binding", str(raised.exception))
        self.assertIn("nothing done", str(raised.exception))
        with self.assertRaises(work.WorkError) as raised:
            self.check(lot["id"], "BG", proof="BG")
        self.assertIn("has no external claim", str(raised.exception))
        self.assertIn("no host gesture is allowed", str(raised.exception))

        self.claim(lot["id"], "BG")
        out = self.check(lot["id"], "BG", proof="BG")
        self.assertEqual(out["session"], "BG")
        self.assertIsNotNone(out["at"])

        # la seconde session est refusée, nommée, et n'obtient aucune écriture
        before = work.get(self.db, lot["id"])["external_claimed_ts"]
        with self.assertRaises(work.WorkError) as raised:
            self.check(lot["id"], "PC", proof="PC")
        self.assertIn("claimed by session BG at", str(raised.exception))
        self.assertIn("host gesture refused", str(raised.exception))
        self.assertEqual(work.get(self.db, lot["id"])["external_claimed_ts"], before)
        # le libellé de la porteuse ne suffit pas sans sa liaison
        with self.assertRaises(work.WorkError) as raised:
            self.check(lot["id"], "BG", proof="PC")
        self.assertIn("verified session is PC, not BG", str(raised.exception))

    def test_la_porte_refuse_un_lot_ordinaire_ou_ferme(self):
        self.external_agent()
        ordinaire = work.add(self.db, title="lot ordinaire", app="nexlink",
                             actor="human:proprio", cfg=self.cfg)
        with self.assertRaises(work.WorkError) as raised:
            self.check(ordinaire["id"], "BG", proof="BG")
        self.assertIn("has no external executor", str(raised.exception))
        lot = self.reserve()
        self.claim(lot["id"], "BG")
        work.close(self.db, lot["id"], abandoned=True, note="test",
                   actor="human:proprio")
        with self.assertRaises(work.WorkError) as raised:
            self.check(lot["id"], "BG", proof="BG")
        self.assertIn("no host gesture to allow", str(raised.exception))

    def test_la_porte_refuse_une_prise_sans_preuve_de_session(self):
        """Une prise écrite par un autre écrivain (preuve NULL) ne fait pas
        autorité : la porte refuse, elle ne suppose pas la session."""
        lot = self.reserve()
        self.claim(lot["id"], "BG")
        self.db.execute(
            "UPDATE work_items SET external_claim_proof = NULL WHERE id = %s",
            (lot["id"],))
        with self.assertRaises(work.WorkError) as raised:
            self.check(lot["id"], "BG", proof="BG")
        self.assertIn("carries no verified session proof", str(raised.exception))

    def test_cli_check_est_la_porte_avant_le_premier_geste(self):
        lot = self.reserve()
        self.bind_session("s-bg")
        proc = self.mesh("work", "claim", str(lot["id"]), "--session", "s-bg",
                         "--actor", "bureau")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        claimed = work.get(self.db, lot["id"])["external_claimed_ts"]

        # sans --session, la porte lit la session VÉRIFIÉE de l'appelant
        proc = self.mesh("work", "claim", str(lot["id"]), "--check", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["schema"], "ameesh.work.claim-check/1")
        self.assertTrue(out["held"])
        self.assertEqual(out["session"], "s-bg")
        self.assertTrue(out["proved"])
        self.assertEqual(work.get(self.db, lot["id"])["external_claimed_ts"], claimed)

        # une autre session est refusée avant son premier geste
        self.db.execute("DELETE FROM session_bindings")
        self.bind_session("s-pc")
        proc = self.mesh("work", "claim", str(lot["id"]), "--check", "--json")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claimed by session s-bg at", proc.stderr)
        self.assertIn("host gesture refused", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["external_claimed_ts"], claimed)

        # --check ne prend ni ne libère
        proc = self.mesh("work", "claim", str(lot["id"]), "--check", "--release")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--check goes alone", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["external_claim_session"], "s-bg")
