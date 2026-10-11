# SPDX-License-Identifier: AGPL-3.0-only
"""L41 (décision 0030, point 6) : liaison explicite des sessions externes.

`ameesh mail bind / unbind / bindings`, import du pont local, hook qui ne
remet le courrier v1 qu'à une session liée (identifiant de session du
harnais, PID ancêtre s'il est lié), rien sans liaison, `whoami` qui dit la
source `session` et la liaison."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from ameesh import identity, registry, session_bindings

from .support import PgTestCase


def _payload(event: str, session: str | None, cwd: str = "/ailleurs") -> str:
    data = {"hook_event_name": event, "cwd": cwd}
    if session is not None:
        data["session_id"] = session
    return json.dumps(data)


class AncestryTest(unittest.TestCase):
    def test_ascendance_du_processus(self):
        chain = session_bindings.ancestors()
        self.assertEqual(chain[0], os.getpid())
        self.assertIn(os.getppid(), chain)
        self.assertNotIn(1, chain)

    def test_validation(self):
        with self.assertRaises(session_bindings.BindError):
            session_bindings.check_harness("vim")
        with self.assertRaises(session_bindings.BindError):
            session_bindings.check_session("avec espace")
        with self.assertRaises(session_bindings.BindError):
            session_bindings.check_pid("1")
        self.assertIsNone(session_bindings.check_pid(None))
        self.assertEqual(session_bindings.check_pid("42"), 42)


class L41LiaisonsTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")

    # -- utilitaires -------------------------------------------------------
    def unbound_env(self, **extra) -> dict:
        env = self.env(**extra)
        env.pop("AGENT_MAIL_NAME", None)
        return env

    def bind(self, *args: str, env: dict | None = None):
        return self.cli("bind", *args, env=env or self.unbound_env())

    def hook(self, session: str | None, *, harness: str = "claude",
             event: str = "UserPromptSubmit", env: dict | None = None):
        return self.cli("hook", harness, env=env or self.unbound_env(),
                        stdin=_payload(event, session))

    def undelivered(self, name: str) -> int:
        return self.db.query(
            "SELECT count(*)::int AS n FROM agent_mailbox "
            "WHERE recipient = %s AND delivered_at IS NULL", (name,))[0]["n"]

    def send(self, dest: str, text: str, sender: str = "beta"):
        proc = self.cli("send", dest, text, "--from", sender, env=self.unbound_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def give_lease(self, name: str) -> None:
        registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = 'runner-x', lease_epoch = 1, "
            "lease_expires_at = now() + interval '1 hour' WHERE name = %s", (name,))

    def active(self) -> list[dict]:
        return session_bindings.listing(self.db)

    # -- bind / unbind / bindings -----------------------------------------
    def test_bind_bindings_unbind(self):
        pid = str(os.getpid())
        proc = self.bind("alpha", "--session", "s-1", "--harness", "claude", "--pid", pid)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("session claude s-1 liée à alpha", proc.stdout)
        # L46 : un agent inconnu est inscrit à la liaison, comme externe
        self.assertIn("inscrit comme agent externe", proc.stderr)
        self.assertEqual(registry.get(self.db, "alpha")["mode"], "externe")

        proc = self.cli("bindings", "--json", env=self.unbound_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        [row] = json.loads(proc.stdout)
        self.assertEqual((row["agent"], row["harness"], row["session_id"], row["pid"],
                          row["host"]), ("alpha", "claude", "s-1", os.getpid(), self.cfg.host))
        self.assertTrue(row["created_by"].startswith("human:"))
        self.assertIsNone(row["revoked_ts"])
        proc = self.cli("bindings", env=self.unbound_env())
        self.assertIn("alpha", proc.stdout)
        self.assertIn("s-1", proc.stdout)

        # idempotent pour le même agent ; le PID se met à jour
        proc = self.bind("alpha", "--session", "s-1", "--harness", "claude", "--pid", pid)
        self.assertIn("déjà liée", proc.stdout)
        proc = self.bind("alpha", "--session", "s-1", "--harness", "claude", "--pid", "4242")
        self.assertIn("re-liée", proc.stdout)
        self.assertEqual([r["pid"] for r in self.active()], [4242])

        # journalisé dans le fil
        excerpts = " ".join(r["last_excerpt"] for r in self.db.query(
            "SELECT last_excerpt FROM thread_index"))
        self.assertIn("liée à alpha", excerpts)

        proc = self.cli("unbind", "--session", "s-1", "--harness", "claude",
                        env=self.unbound_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("liaison révoquée", proc.stdout)
        self.assertEqual(self.active(), [])
        self.assertIn("aucune liaison", self.cli("bindings", env=self.unbound_env()).stdout)
        proc = self.cli("bindings", "--all", "--json", env=self.unbound_env())
        [row] = json.loads(proc.stdout)
        self.assertIsNotNone(row["revoked_ts"])
        proc = self.cli("unbind", "--session", "s-1", "--harness", "claude",
                        env=self.unbound_env())
        self.assertEqual(proc.returncode, 1)
        # révoquée, la session se relie (à un autre agent, même)
        proc = self.bind("beta", "--session", "s-1", "--harness", "claude")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pid non contrôlé", proc.stdout)

    def test_bind_par_ameesh_mail(self):
        proc = self.mesh("mail", "bind", "alpha", "--session", "s-9", "--harness", "codex",
                         env=self.unbound_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([(r["agent"], r["harness"]) for r in self.active()],
                         [("alpha", "codex")])
        proc = self.mesh("mail", "bind", "--help", env=self.unbound_env())
        self.assertEqual(proc.returncode, 0)
        self.assertIn("agent-mail bind <NOM> --session ID", proc.stdout)

    def test_refus_bail_vivant(self):
        self.give_lease("mene")
        proc = self.bind("mene", "--session", "s-2", "--harness", "codex")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("bail vivant", proc.stderr)
        self.assertEqual(self.active(), [])

    def test_refus_session_deja_liee_a_un_autre(self):
        self.assertEqual(self.bind("alpha", "--session", "s-3", "--harness",
                                   "claude").returncode, 0)
        proc = self.bind("beta", "--session", "s-3", "--harness", "claude")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("déjà liée à alpha", proc.stderr)
        # même identifiant, autre harnais : autre session
        self.assertEqual(self.bind("beta", "--session", "s-3", "--harness",
                                   "codex").returncode, 0)
        self.assertEqual(sorted((r["agent"], r["harness"]) for r in self.active()),
                         [("alpha", "claude"), ("beta", "codex")])

    def test_arguments_invalides(self):
        self.assertEqual(self.bind("alpha", "--session", "s", "--harness", "vim").returncode, 1)
        self.assertEqual(self.bind("alpha", "--session", "s", "--harness", "claude",
                                   "--pid", "zéro").returncode, 1)
        self.assertEqual(self.bind("alpha", "--harness", "claude").returncode, 2)
        self.assertEqual(self.bind("--session", "s", "--harness", "claude").returncode, 2)
        self.assertEqual(self.bind("a b", "--session", "s", "--harness", "claude").returncode, 1)
        self.assertEqual(self.active(), [])

    def test_import_du_pont_local(self):
        self.give_lease("mene")
        self.assertEqual(self.bind("autre", "--session", "s-pris", "--harness",
                                   "claude").returncode, 0)
        path = os.path.join(self.tmp, "external-session-bindings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "sessions": {
                "s-ok": {"name": "auteur-externe", "pid": 329470, "harness": "claude",
                         "cwd": "/home/x/chantier"},
                "s-sans-pid": {"name": "acme-docs", "harness": "claude"},
                "s-codex": {"name": "mene", "pid": 713826, "harness": "codex"},
                "s-pris": {"name": "orchestrateur", "pid": 299586, "harness": "claude"},
                "s-vim": {"name": "x", "pid": 12, "harness": "vim"},
                "s-nom": {"name": "a b", "harness": "claude"},
                "s-texte": "illisible",
            }}, fh)
        proc = self.bind("--import", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("liaisons importées : 2 ; ignorées : 5", proc.stdout)
        self.assertIn("importée : s-ok → auteur-externe (claude, pid 329470)", proc.stdout)
        self.assertIn("importée : s-sans-pid → acme-docs (claude, pid non contrôlé)",
                      proc.stdout)
        self.assertRegex(proc.stdout, r"ignorée  : s-codex — .*bail vivant")
        self.assertRegex(proc.stdout, r"ignorée  : s-pris — .*déjà liée à autre")
        self.assertRegex(proc.stdout, r"ignorée  : s-vim — harnais invalide")
        self.assertRegex(proc.stdout, r"ignorée  : s-nom — nom d'agent invalide")
        self.assertRegex(proc.stdout, r"ignorée  : s-texte — entrée illisible")
        actives = {r["session_id"]: r for r in self.active()}
        self.assertEqual(sorted(actives), ["s-ok", "s-pris", "s-sans-pid"])
        self.assertEqual(actives["s-ok"]["pid"], 329470)
        self.assertIsNone(actives["s-sans-pid"]["pid"])

        # un second import ne change rien
        proc = self.bind("--import", path)
        self.assertIn("liaisons importées : 0 ; ignorées : 7", proc.stdout)
        self.assertIn("déjà liée à auteur-externe (identique)", proc.stdout)

        self.assertEqual(self.bind("--import", os.path.join(self.tmp, "absent.json"))
                         .returncode, 1)
        self.assertEqual(self.bind("--import", path, "alpha").returncode, 2)

    # -- hook -------------------------------------------------------------
    def test_hook_avec_liaison_remet_le_courrier_v1(self):
        self.bind("alpha", "--session", "s-1", "--harness", "claude",
                  "--pid", str(os.getpid()))
        self.send("alpha", "Courrier v1 pour alpha.")
        proc = self.hook("s-1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertIn("Courrier v1 pour alpha.",
                      out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.undelivered("alpha"), 0)
        row = registry.get(self.db, "alpha")
        self.assertEqual((row["harness"], row["session_id"]), ("claude", "s-1"))
        # Stop : relance avec le courrier suivant
        self.send("alpha", "Encore un message.")
        out = json.loads(self.hook("s-1", event="Stop").stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("Encore un message.", out["reason"])

    def test_hook_lie_ne_remplace_pas_la_session_enregistree(self):
        # L36 : la source `session` n'est pas un bail
        self.register("alpha", "claude", cwd=self.tmp, session="session-du-runner")
        # L46 : un agent `execute` ne se lie que de force
        proc = self.bind("alpha", "--session", "s-ext", "--harness", "claude")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("agent mené par l'exécuteur ; une session externe lui volerait "
                      "son courrier", proc.stderr)
        proc = self.bind("alpha", "--session", "s-ext", "--harness", "claude", "--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.send("alpha", "Bonjour.")
        proc = self.hook("s-ext", event="SessionStart")
        self.assertIn("Bonjour.", proc.stdout)
        row = registry.get(self.db, "alpha")
        self.assertEqual(row["session_id"], "session-du-runner")
        self.assertEqual(row["cwd"], self.tmp)

    def test_hook_sans_liaison_ne_remet_rien_et_n_ecrit_rien(self):
        registry.upsert(self.db, "alpha", harness="claude", host=self.cfg.host)
        self.bind("alpha", "--session", "s-1", "--harness", "claude")
        self.send("alpha", "Pour alpha seulement.")
        before = self.db.query("SELECT name, session_id, cwd, last_seen FROM agent_registry "
                               "ORDER BY name")
        for session, harness in (("s-inconnue", "claude"), (None, "claude"),
                                 ("s-1", "codex"), ("", "claude")):
            proc = self.hook(session, harness=harness)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "", (session, harness))
        # le dossier ne donne rien non plus, même nommé comme l'agent
        dossier = os.path.join(self.tmp, "alpha")
        os.makedirs(dossier, exist_ok=True)
        proc = subprocess.run(
            [sys.executable, "-m", "ameesh.cli", "hook", "claude"], input=_payload("UserPromptSubmit", "s-x", dossier),
            capture_output=True, text=True, env=self.unbound_env(), cwd=dossier, timeout=60)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(self.undelivered("alpha"), 1)
        after = self.db.query("SELECT name, session_id, cwd, last_seen FROM agent_registry "
                              "ORDER BY name")
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(os.path.join(self.state, "hooks")))

    def test_pid_hors_ascendance_rien(self):
        sleeper = subprocess.Popen(["sleep", "60"])
        try:
            self.bind("alpha", "--session", "s-1", "--harness", "claude",
                      "--pid", str(sleeper.pid))
            self.send("alpha", "Ne doit pas sortir.")
            proc = self.hook("s-1")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            self.assertIn("ascendance", proc.stderr)
            self.assertEqual(self.undelivered("alpha"), 1)
            # inscrit par la liaison (L46 : externe) : le hook n'a rien enregistré
            row = registry.get(self.db, "alpha")
            self.assertEqual((row["harness"], row["session_id"], row["mode"]),
                             ("claude", None, "externe"))
            proc = self.cli("whoami", env=self.unbound_env())
            self.assertEqual(proc.returncode, 1)
        finally:
            sleeper.kill()
            sleeper.wait()

    def test_hook_refuse_si_l_agent_a_pris_un_bail(self):
        self.bind("alpha", "--session", "s-1", "--harness", "claude")
        self.send("alpha", "Pour l'exécuteur.")
        self.give_lease("alpha")
        proc = self.hook("s-1")
        self.assertEqual(proc.stdout, "")
        self.assertIn("mené par l'exécuteur", proc.stderr)
        self.assertEqual(self.undelivered("alpha"), 1)

    def test_agent_mail_name_reste_prioritaire(self):
        self.bind("alpha", "--session", "s-1", "--harness", "claude")
        registry.upsert(self.db, "gamma", harness="claude", host=self.cfg.host)
        self.send("gamma", "Pour gamma.")
        self.send("alpha", "Pour alpha.")
        proc = self.hook("s-1", env=self.env(AGENT_MAIL_NAME="gamma"))
        self.assertIn("Pour gamma.", proc.stdout)
        self.assertNotIn("Pour alpha.", proc.stdout)

    # -- whoami et commandes dans une session liée ---------------------------
    def test_whoami_source_session(self):
        env = self.unbound_env()
        proc = self.cli("whoami", env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("identité non liée", proc.stderr)

        self.bind("alpha", "--session", "s-1", "--harness", "claude",
                  "--pid", str(os.getpid()))
        proc = self.cli("whoami", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(),
                         "alpha [session] liaison claude s-1 sur %s, pid %d, par %s"
                         % (self.cfg.host, os.getpid(), self.active()[0]["created_by"]))
        proc = self.cli("whoami", "--session", "s-1", "--harness", "claude", env=env)
        self.assertTrue(proc.stdout.startswith("alpha [session] liaison claude s-1"))
        proc = self.cli("whoami", "--session", "s-2", "--harness", "claude", env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("non liée", proc.stderr)
        # le diagnostic de dossier reste un diagnostic
        proc = self.cli("whoami", "--cwd", self.tmp, env=env)
        self.assertIn("non autoritaire", proc.stdout)
        # une commande lancée dans la session liée parle en son nom
        registry.upsert(self.db, "beta", harness="codex", host=self.cfg.host)
        proc = self.cli("send", "beta", "Réponse d'alpha.", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        [row] = self.db.query("SELECT sender FROM agent_mailbox WHERE recipient = 'beta'")
        self.assertEqual(row["sender"], "alpha")

    def test_resolution_directe(self):
        self.bind("alpha", "--session", "s-1", "--harness", "deepseek")
        # l'identité du lanceur de la suite ne compte pas ici
        propre = {k: v for k, v in os.environ.items()
                  if k not in ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AMEESH_LEASE_EPOCH")}
        with mock.patch.dict(os.environ, propre, clear=True):
            binding = identity.resolve_binding(self.cfg, self.db, harness="deepseek",
                                               session_id="s-1")
            sans_base = identity.resolve_binding(self.cfg, None, harness="deepseek",
                                                 session_id="s-1")
        self.assertTrue(binding.ok, binding.reason)
        self.assertEqual((binding.name, binding.source), ("alpha", "session"))
        self.assertFalse(binding.bound_to_lease)
        self.assertFalse(sans_base.ok)


if __name__ == "__main__":
    unittest.main()
