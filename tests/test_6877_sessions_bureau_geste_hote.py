# SPDX-License-Identifier: AGPL-3.0-only
"""#6877 : deux sessions du bureau (BG et PC) partagent l'identité et la clé
SSH de BG ; « exécutant unique » sans garde mécanique.

Le constat du 11/11 sur manaty-its-node : apply-services et un lancement de
services ont été faits depuis l'IP de BG avec sa clé générale, sans qu'aucune
session ne les revendique ; sshd ne distingue pas les deux sessions. Le lot
ajoute deux pièces mécaniques, sans toucher aux clés (décision owner #211859) :

* le fil nomme la session VÉRIFIÉE qui écrit (L41, PID ancêtre), au lieu de
  la seule identité « bureau » ;
* `ameesh work claim <lot> --check` est la porte, en lecture seule, qu'un
  runbook appelle avant son premier geste hôte : elle compare l'identité
  COMPLÈTE de la liaison (session, agent, hôte, harnais, ligne de liaison) à
  celle écrite dans la prise, pas un libellé réutilisable.

Les clés SSH séparées par session sont la décision owner #211859 option a
(lots #6881/#6882/#6883) : ce lot ne les applique pas."""
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

    def bind_session(self, session_id, *, agent="bureau", host=None,
                     harness="claude"):
        """Une liaison L41 active, PID ancêtre du processus de test ; rend sa
        ligne (l'identité complète de la preuve)."""
        host = host or self.cfg.host
        # une session externe ne se lie pas à un agent mené par un bail (L41)
        self.db.execute("UPDATE agent_registry SET lease_owner = NULL,"
                        " lease_expires_at = NULL, status = 'idle'"
                        " WHERE name = %s", (agent,))
        self.db.execute(
            "INSERT INTO session_bindings"
            " (session_id, harness, host, agent, pid, created_by)"
            " VALUES (%s, %s, %s, %s, %s, 'test')",
            (session_id, harness, host, agent, os.getpid()))
        [row] = self.db.query(
            "SELECT id, session_id, harness, host, agent FROM session_bindings"
            " WHERE session_id = %s AND harness = %s AND host = %s"
            "   AND revoked_at IS NULL", (session_id, harness, host))
        return row

    def proof_of(self, row):
        """La preuve complète d'une liaison, telle que `caller_session_proof`
        la rend (la ligne de liaison comprise)."""
        return {"session": row["session_id"], "agent": row["agent"],
                "host": row["host"], "harness": row["harness"],
                "binding": int(row["id"])}

    def claim(self, lot_id, session, *, proof=None, actor="bureau", host=None):
        return work.claim_external(self.db, lot_id, session, actor=actor,
                                   host=host, proof=proof, cfg=self.cfg)

    def release(self, lot_id, session, *, proof=None, actor="bureau", host=None):
        return work.release_external(self.db, lot_id, session, actor=actor,
                                     host=host, proof=proof, cfg=self.cfg)

    def check(self, lot_id, session=None, *, proof=None):
        return work.check_external_claim(self.db, lot_id, session, proof=proof)

    def test_la_porte_exige_l_identite_complete_et_refuse_la_seconde_session(self):
        lot = self.reserve()
        for proof in (None, "BG", {"session": "BG", "agent": "bureau"}):
            with self.assertRaises(work.WorkError) as raised:
                self.check(lot["id"], "BG", proof=proof)
            self.assertIn("full identity", str(raised.exception))
            self.assertIn("nothing done", str(raised.exception))

        bg = self.bind_session("s-bg")
        with self.assertRaises(work.WorkError) as raised:
            self.check(lot["id"], proof=self.proof_of(bg))
        self.assertIn("has no external claim", str(raised.exception))
        self.claim(lot["id"], "s-bg", proof=self.proof_of(bg))
        out = self.check(lot["id"], proof=self.proof_of(bg))
        self.assertEqual(out["session"], "s-bg")
        self.assertEqual(out["agent"], "bureau")
        self.assertEqual(out["host"], self.cfg.host)
        self.assertEqual(out["harness"], "claude")
        self.assertEqual(out["binding"], int(bg["id"]))
        self.assertIsNotNone(out["at"])

        # la seconde session, pourtant liée sur le même hôte, est refusée
        pc = self.bind_session("s-pc")
        before = work.get(self.db, lot["id"])["external_claimed_ts"]
        with self.assertRaises(work.WorkError) as raised:
            self.check(lot["id"], proof=self.proof_of(pc))
        self.assertIn("claimed by session s-bg at", str(raised.exception))
        self.assertIn("host gesture refused", str(raised.exception))
        self.assertEqual(work.get(self.db, lot["id"])["external_claimed_ts"], before)
        # le libellé de la porteuse, sans sa liaison, ne passe pas non plus
        with self.assertRaises(work.WorkError) as raised:
            self.check(lot["id"], "s-bg", proof="s-bg")
        self.assertIn("full identity", str(raised.exception))

    def test_le_meme_libelle_sur_un_autre_hote_ne_prend_pas_la_prise(self):
        """Le scénario B1 : même agent, même harnais, même identifiant de
        session, mais une liaison réelle sur un AUTRE hôte."""
        lot = self.reserve()
        bg = self.bind_session("s-bureau")
        self.claim(lot["id"], "s-bureau", proof=self.proof_of(bg))
        # la liaison PC est valide sur son hôte (l'unicité est locale à l'hôte)
        pc = self.bind_session("s-bureau", host="pc-smichea")
        with self.assertRaises(work.WorkError) as raised:
            self.check(lot["id"], proof=self.proof_of(pc))
        message = str(raised.exception)
        self.assertIn("claimed on host %s, not pc-smichea" % self.cfg.host, message)
        self.assertIn("nothing done", message)
        # la porteuse inchangée passe toujours
        self.assertEqual(self.check(lot["id"], proof=self.proof_of(bg))["session"],
                         "s-bureau")

    def test_le_meme_libelle_sous_un_autre_agent_ou_harnais_est_refuse(self):
        lot = self.reserve()
        bg = self.bind_session("s-bg")
        self.claim(lot["id"], "s-bg", proof=self.proof_of(bg))
        # même hôte et même identifiant, autre harnais : liaison distincte
        codex = self.bind_session("s-bg", harness="codex")
        with self.assertRaises(work.WorkError) as raised:
            self.check(lot["id"], proof=self.proof_of(codex))
        self.assertIn("claimed with harness claude, not codex",
                      str(raised.exception))
        # une preuve qui se déclare sous un autre agent est refusée
        forged = dict(self.proof_of(bg))
        forged["agent"] = "autre"
        with self.assertRaises(work.WorkError) as raised:
            self.check(lot["id"], proof=forged)
        self.assertIn("claimed by agent bureau, not autre", str(raised.exception))

    def test_une_liaison_refaite_est_une_autre_ligne_et_ne_prend_pas_la_prise(self):
        """Révoquer puis relier la même session refait une ligne : la prise
        reste à l'ancienne, la nouvelle doit la libérer puis reprendre."""
        lot = self.reserve()
        bg = self.bind_session("s-bg")
        self.claim(lot["id"], "s-bg", proof=self.proof_of(bg))
        self.db.execute("UPDATE session_bindings SET revoked_at = now()"
                        " WHERE id = %s", (bg["id"],))
        again = self.bind_session("s-bg")
        self.assertNotEqual(again["id"], bg["id"])
        with self.assertRaises(work.WorkError) as raised:
            self.check(lot["id"], proof=self.proof_of(again))
        message = str(raised.exception)
        self.assertIn("binding line %s, not by this caller's line %s"
                      % (bg["id"], again["id"]), message)
        self.assertIn("pilot uses --force", message)
        # la levée par la nouvelle liaison est refusée pour la même raison
        with self.assertRaises(work.WorkError) as raised:
            self.release(lot["id"], "s-bg", proof=self.proof_of(again))
        self.assertIn("another binding (line %s)" % bg["id"], str(raised.exception))

    def test_une_prise_sans_identite_complete_est_refusee(self):
        """Une prise écrite avec le seul libellé (compatibilité N-1) ou par un
        autre écrivain ne fait pas autorité : la porte refuse et dit comment
        la refaire."""
        lot = self.reserve()
        self.claim(lot["id"], "BG", proof="BG")      # ancien libellé seul
        bg = self.bind_session("BG")
        with self.assertRaises(work.WorkError) as raised:
            self.check(lot["id"], proof=self.proof_of(bg))
        self.assertIn("carries no full verified identity", str(raised.exception))
        self.assertIn("release it and claim again", str(raised.exception))

    def test_cli_check_est_la_porte_avant_le_premier_geste(self):
        lot = self.reserve()
        bg = self.bind_session("s-bg")
        proc = self.mesh("work", "claim", str(lot["id"]), "--session", "s-bg",
                         "--actor", "bureau")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stored = work.get(self.db, lot["id"])
        claimed = stored["external_claimed_ts"]
        self.assertEqual(int(stored["external_claim_binding"]), int(bg["id"]))
        self.assertEqual(stored["external_claim_harness"], "claude")

        # sans --session, la porte lit la session VÉRIFIÉE de l'appelant
        proc = self.mesh("work", "claim", str(lot["id"]), "--check", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["schema"], "ameesh.work.claim-check/1")
        self.assertTrue(out["held"])
        self.assertEqual(out["session"], "s-bg")
        self.assertEqual(out["agent"], "bureau")
        self.assertEqual(out["host"], self.cfg.host)
        self.assertEqual(out["harness"], "claude")
        self.assertEqual(int(out["binding"]), int(bg["id"]))
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
