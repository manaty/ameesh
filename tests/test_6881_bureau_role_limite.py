# SPDX-License-Identifier: AGPL-3.0-only
"""#6881 : la connexion PostgreSQL limitée du bureau (bureau-goal).

Décision owner #211859 a : CLI ameesh et accès base sur bureau-goal, avec un
rôle limité. Les témoins posent `deploy/sql/role-bureau.sql` sur un schéma
migré, puis jouent les VRAIES commandes du bureau sous cette connexion (DSN du
rôle, aucun autre) : `mail bind` avec PID vérifié, `work claim` (#6839),
`agent-mail send|inbox`, `work show`, et un vrai `git commit` à travers les
hooks de provenance installés, qui écrit son reçu. Puis les refus attendus,
chacun témoigné : DDL, tables d'approbation et de clés, écriture du registre,
courrier ou reçu au nom d'un autre, prise d'une autre session ou d'un lot non
réservé, changement d'état d'un lot, événement direct, suppression, SET ROLE ;
et l'audit qui refuse un droit en trop.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import uuid

from ameesh import db as db_mod
from ameesh import work

from .role_acl import serialize_role_acl
from .support import REPO, TEST_DSN, PgTestCase, mechanics_canon

SQL = os.path.join(REPO, "deploy", "sql")


def _dsn_for(user: str, password: str) -> str:
    parts = urllib.parse.urlsplit(TEST_DSN)
    if parts.scheme not in ("postgresql", "postgres") or not parts.hostname:
        raise unittest.SkipTest("AMEESH_TEST_DSN n'est pas une URL : connexion limitée sautée")
    host = parts.hostname + (":%d" % parts.port if parts.port else "")
    netloc = "%s:%s@%s" % (urllib.parse.quote(user), urllib.parse.quote(password), host)
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


class BureauRoleLimiteTest(PgTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        serialize_role_acl(cls)
        cls.login = "t_bureau_%s" % uuid.uuid4().hex[:10]
        cls.password = secrets.token_hex(16)  # éphémère, détruit avec le rôle
        cls.addClassCleanup(cls.drop_role)
        for _ in range(2):  # idempotent : la seconde passe ne change rien
            proc = cls.script("role-bureau.sql")
            if proc.returncode:
                raise AssertionError(proc.stderr)
        cls.db.execute("ALTER ROLE %s PASSWORD '%s'"
                       % (db_mod.quote_ident(cls.login), cls.password))
        cls.dsn = _dsn_for(cls.login, cls.password)

    @classmethod
    def drop_role(cls) -> None:
        subprocess.run(["psql", "-X", "-w", TEST_DSN, "-q", "-c",
                        'DROP OWNED BY "%s" CASCADE; DROP ROLE IF EXISTS "%s";'
                        % (cls.login, cls.login)],
                       capture_output=True, text=True, timeout=30)

    @classmethod
    def script(cls, name: str, **extra) -> subprocess.CompletedProcess:
        variables = {"role": cls.login, "agent": "bureau", "host": cls.cfg.host}
        variables.update(extra)
        args = ["psql", "-X", "-w", TEST_DSN, "-q", "-v", "ON_ERROR_STOP=1"]
        for key, value in variables.items():
            args += ["-v", "%s=%s" % (key, value)]
        return subprocess.run(args + ["-f", os.path.join(SQL, name)],
                              env=dict(os.environ, PGOPTIONS="-c search_path=" + cls.schema),
                              capture_output=True, text=True, timeout=120)

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")
        for name in ("bureau", "orchestrateur"):
            self.db.execute(
                "INSERT INTO agent_registry (name, harness, host, mode, responsible)"
                " VALUES (%s, 'claude', %s, 'externe', 'human:proprio')"
                " ON CONFLICT (name) DO UPDATE SET mode = 'externe', harness = 'claude',"
                " lease_owner = NULL, lease_expires_at = NULL, status = 'idle'",
                (name, self.cfg.host))

    # -- utilitaires -------------------------------------------------------
    def limited_env(self, **extra) -> dict:
        env = self.env(AMEESH_DSN=self.dsn, **extra)
        for name in ("AGENT_MAIL_NAME", "PGPASSWORD", "PGOPTIONS", "AGENT_MESH_DSN",
                     "AMEESH_DATABASE_URL", "AGENT_MESH_DATABASE_URL"):
            env.pop(name, None)
        return env

    def as_bureau(self, *args: str, **extra) -> subprocess.CompletedProcess:
        return self.mesh(*args, env=self.limited_env(**extra))

    def sql(self, statement: str) -> subprocess.CompletedProcess:
        """Une instruction SQL brute sous la connexion limitée (hors CLI)."""
        env = dict(os.environ, PGOPTIONS="-c search_path=" + self.schema)
        env.pop("PGPASSWORD", None)
        return subprocess.run(["psql", self.dsn, "-X", "-At", "-v", "ON_ERROR_STOP=1",
                               "-c", statement],
                              capture_output=True, text=True, timeout=30, env=env)

    def refused(self, statement: str, needle: str = "") -> str:
        proc = self.sql(statement)
        self.assertNotEqual(proc.returncode, 0, "accepté : %s\n%s" % (statement, proc.stdout))
        if needle:
            self.assertIn(needle, proc.stderr)
        return proc.stderr

    def bind(self, session: str) -> None:
        proc = self.as_bureau("mail", "bind", "bureau", "--session", session,
                              "--harness", "claude", "--pid", str(os.getpid()))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pid %d" % os.getpid(), proc.stdout)

    def reserve(self, executor: str = "bureau") -> int:
        lot = work.add(self.db, title="geste hôte du bureau", app="ameesh",
                       actor="human:proprio", cfg=self.cfg, external_executor=executor)
        return int(lot["id"])

    # -- installation et audit ---------------------------------------------
    def test_l_audit_seul_passe_puis_refuse_un_droit_en_trop(self):
        proc = self.script("_contrat-bureau.sql")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.db.execute('GRANT SELECT ON mesh_approvals TO "%s"' % self.login)
        try:
            proc = self.script("_contrat-bureau.sql")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("unexpected SELECT on mesh_approvals", proc.stderr)
        finally:
            self.db.execute('REVOKE SELECT ON mesh_approvals FROM "%s"' % self.login)
        self.db.execute('GRANT UPDATE (state) ON work_items TO "%s"' % self.login)
        try:
            proc = self.script("_contrat-bureau.sql")
            self.assertIn("unexpected UPDATE on work_items.state", proc.stderr)
            # la réinstallation remet à zéro les droits directs
            proc = self.script("role-bureau.sql")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(self.script("_contrat-bureau.sql").returncode, 0)
        finally:
            self.db.execute('REVOKE UPDATE (state) ON work_items FROM "%s"' % self.login)

    def test_une_appartenance_en_plus_est_refusee(self):
        other = "t_bur_x_%s" % uuid.uuid4().hex[:8]
        self.db.execute('CREATE ROLE "%s" NOLOGIN' % other)
        try:
            self.db.execute('GRANT "%s" TO "%s"' % (other, self.login))
            proc = self.script("_contrat-bureau.sql")
            self.assertIn("role membership", proc.stderr)
        finally:
            self.db.execute('DROP ROLE "%s"' % other)

    # -- les gestes du bureau, sous la connexion limitée --------------------
    def test_liaison_prise_courrier_et_lecture_du_lot(self):
        lot = self.reserve()
        self.bind("BG")
        proc = self.as_bureau("work", "claim", str(lot), "--session", "BG")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot)["external_claim_session"], "BG")
        # une seconde session liée est refusée par la prise (#6839)…
        self.bind("PC")
        proc = self.as_bureau("work", "claim", str(lot), "--session", "PC")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("already taken by BG", proc.stderr)
        # … et la base refuse le contournement direct de la prise
        self.refused("UPDATE work_items SET external_claim_session = 'PC',"
                     " external_claim_proof = 'PC' WHERE id = %d" % lot,
                     "claim of lot %d refused" % lot)
        self.assertEqual(work.get(self.db, lot)["external_claim_session"], "BG")
        proc = self.as_bureau("work", "show", str(lot))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("geste hôte du bureau", proc.stdout)
        # deux vraies sessions ont deux PID ; ici les deux liaisons portent
        # celui du banc : le bureau révoque PC (`mail unbind`) pour que BG soit
        # la session vérifiée ; rouvrir une liaison révoquée lui est refusé
        proc = self.as_bureau("mail", "unbind", "--session", "PC", "--harness", "claude")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.refused("UPDATE session_bindings SET revoked_at = NULL WHERE session_id = 'PC'",
                     "binds bureau")
        proc = self.as_bureau("work", "claim", str(lot), "--session", "BG", "--release")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(work.get(self.db, lot)["external_claim_session"])
        # courrier : envoi signé du bureau, lecture de sa boîte
        proc = self.as_bureau("mail", "send", "orchestrateur", "geste pris puis rendu",
                              "--from", "bureau", "--lot", str(lot))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.db.execute("INSERT INTO agent_mailbox (sender, recipient, body)"
                        " VALUES ('orchestrateur', 'bureau', 'reçu')")
        proc = self.as_bureau("mail", "inbox", "bureau")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reçu", proc.stdout)

    def test_prise_identite_complete_check_puis_levee(self):
        """#6877 (0566) sur l'arbre intégré : la prise écrit le harnais et la
        ligne de liaison sous la connexion limitée, `--check` la voit, la
        levée l'efface ; la base refuse une ligne de liaison qui n'est pas la
        liaison active de la session. Sur un arbre sans 0566, la prise
        ancienne seule est témoignée."""
        lot = self.reserve()
        self.bind("BG")
        proc = self.as_bureau("work", "claim", str(lot), "--session", "BG")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item = work.get(self.db, lot)
        self.assertEqual(item["external_claim_session"], "BG")
        integrated = "external_claim_binding" in item
        if integrated:
            own = self.db.query("SELECT id FROM session_bindings WHERE session_id = 'BG'"
                                " AND revoked_at IS NULL")[0]["id"]
            self.assertEqual(item["external_claim_harness"], "claude")
            self.assertEqual(int(item["external_claim_binding"]), int(own))
            proc = self.as_bureau("work", "claim", str(lot), "--check")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("held by BG", proc.stdout)
            self.bind("PC")
            other = self.db.query("SELECT id FROM session_bindings WHERE session_id = 'PC'")[0]["id"]
            self.refused("UPDATE work_items SET external_claim_binding = %d"
                         " WHERE id = %d" % (other, lot), "not its active one")
            self.refused("UPDATE work_items SET external_claim_harness = NULL"
                         " WHERE id = %d" % lot, "harness and binding together")
            proc = self.as_bureau("mail", "unbind", "--session", "PC", "--harness", "claude")
            self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.as_bureau("work", "claim", str(lot), "--session", "BG", "--release")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item = work.get(self.db, lot)
        self.assertIsNone(item["external_claim_session"])
        if integrated:
            self.assertIsNone(item["external_claim_binding"])
            self.assertIsNone(item["external_claim_harness"])

    def test_le_hook_agent_mail_ne_touche_a_rien(self):
        """Hors périmètre (#6881) : l'inscription du hook écrit le registre,
        fermé à la connexion limitée. Le hook ne fait pas échouer la session
        et laisse le courrier non lu ; le bureau lit par `mail inbox`."""
        self.bind("BG")
        self.db.execute("INSERT INTO agent_mailbox (sender, recipient, body)"
                        " VALUES ('orchestrateur', 'bureau', 'geste à prendre')")
        before = self.db.query("SELECT cwd, session_id, last_seen FROM agent_registry"
                               " WHERE name = 'bureau'")
        proc = subprocess.run(
            [sys.executable, "-c", "import sys; sys.argv = ['agent-mail', 'hook', 'claude'];"
             " import ameesh.entry as e; sys.exit(e.main())"],
            input=json.dumps({"hook_event_name": "Stop", "cwd": self.tmp,
                              "session_id": "BG"}),
            capture_output=True, text=True, timeout=60, cwd=self.tmp,
            env=self.limited_env(AGENT_MAIL_NAME="bureau"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.db.query("SELECT cwd, session_id, last_seen FROM agent_registry"
                                       " WHERE name = 'bureau'"), before)
        row = self.db.query("SELECT status, delivered_at FROM agent_mailbox"
                            " WHERE recipient = 'bureau'")[0]
        self.assertIsNone(row["delivered_at"])
        self.refused("UPDATE agent_registry SET last_seen = now() WHERE name = 'bureau'",
                     "permission denied")

    def test_commit_par_les_hooks_installes_avec_recu(self):
        from ameesh import review_provenance as prov
        self.bind("BG")
        repo = tempfile.mkdtemp(prefix="ameesh-6881-", dir=self.tmp)
        subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
        self.assertTrue(prov.install_hook(repo)["installed"])
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        wrapper = os.path.join(bindir, "ameesh")
        with open(wrapper, "w", encoding="utf-8") as fh:
            fh.write("#!%s\nimport sys\nfrom ameesh.main import main\n"
                     "sys.exit(main())\n" % sys.executable)
        os.chmod(wrapper, 0o755)
        canon = mechanics_canon(os.path.join(self.tmp, "canon"))
        env = self.limited_env(AMEESH_CANON=canon, AMEESH_CANON_REF="HEAD",
                               PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
        env.update(GIT_AUTHOR_NAME="bureau", GIT_AUTHOR_EMAIL="bureau@ameesh.invalid",
                   GIT_COMMITTER_NAME="bureau", GIT_COMMITTER_EMAIL="bureau@ameesh.invalid")
        with open(os.path.join(repo, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True, capture_output=True)
        commit = subprocess.run(["git", "commit", "-q", "-m", "commit de contrôle"],
                                cwd=repo, capture_output=True, text=True, env=env)
        self.assertEqual(commit.returncode, 0, commit.stderr)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo,
                                       text=True).strip()
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertIsNotNone(receipt, "les hooks n'ont pas écrit le reçu : %s" % commit.stderr)
        self.assertEqual((receipt["actor"], receipt["session"]), ("bureau", "BG"))

    def test_le_controle_final_du_script_d_installation(self):
        """`installer-bureau-goal.sh controler` : liaison avec PID vérifié, prise
        refusée à une seconde session, commit de contrôle avec reçu."""
        lot = self.reserve()
        home = tempfile.mkdtemp(prefix="ameesh-6881-home-", dir=self.tmp)
        base = os.path.join(home, "base")
        for folder in (os.path.join(base, "venv", "bin"), os.path.join(home, ".local", "bin")):
            os.makedirs(folder, exist_ok=True)
            for name, module in (("ameesh", "ameesh.main"), ("agent-mail", "ameesh.entry")):
                path = os.path.join(folder, name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("#!%s\nimport sys\nfrom %s import main\nsys.exit(main())\n"
                             % (sys.executable, module))
                os.chmod(path, 0o755)
        canon = mechanics_canon(os.path.join(self.tmp, "canon-script"))
        env = self.limited_env(HOME=home, BASE=base, HOST_NAME=self.cfg.host,
                               AMEESH_CANON=canon, AMEESH_CANON_REF="HEAD")
        proc = subprocess.run(
            ["bash", os.path.join(REPO, "deploy", "bureau", "installer-bureau-goal.sh"),
             "controler", str(lot)],
            capture_output=True, text=True, timeout=300, env=env, cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("refus attendu témoigné", proc.stdout)
        self.assertIn("already taken by controle-a-", proc.stdout + proc.stderr)
        self.assertIn("contrôle complet", proc.stdout)
        self.assertIsNone(work.get(self.db, lot)["external_claim_session"])
        self.assertEqual(self.db.query("SELECT count(*)::int AS n FROM session_bindings"
                                       " WHERE revoked_at IS NULL")[0]["n"], 0)

    # -- refus témoignés ------------------------------------------------------
    def test_aucun_ddl_ni_secret_ni_approbation(self):
        self.refused("CREATE TABLE intrus (x int)", "permission denied")
        self.refused("CREATE TEMP VIEW v AS SELECT 1; CREATE FUNCTION f() RETURNS int"
                     " LANGUAGE sql AS 'select 1'", "permission denied")
        self.refused("ALTER TABLE work_items ADD COLUMN intrus int")
        self.refused("SELECT count(*) FROM mesh_approvals", "permission denied")
        self.refused("SELECT count(*) FROM standing_approvals", "permission denied")
        self.refused("SELECT count(*) FROM key_holders", "permission denied")
        self.refused("SET ROLE ameesh_login_limite", "permission denied")
        self.refused("SET session_replication_role = replica")

    def test_ecritures_hors_bornes_refusees_par_la_base(self):
        lot = self.reserve()
        other = self.reserve(executor="orchestrateur")
        self.bind("BG")
        self.refused("UPDATE agent_registry SET updated_at = now() WHERE name = 'bureau'",
                     "no write on agent_registry")
        self.refused("UPDATE work_items SET state = 'merged' WHERE id = %d" % lot,
                     "permission denied")
        self.refused("UPDATE work_items SET external_claim_session = 'BG',"
                     " external_claimed_at = now(), external_claim_actor = 'bureau',"
                     " external_claim_host = '%s', external_claim_proof = 'BG'"
                     " WHERE id = %d" % (self.cfg.host, other),
                     "is not reserved to bureau")
        self.refused("UPDATE work_items SET external_claim_session = 'XX',"
                     " external_claimed_at = now(), external_claim_actor = 'bureau',"
                     " external_claim_host = '%s', external_claim_proof = 'XX'"
                     " WHERE id = %d" % (self.cfg.host, lot), "refused")
        self.refused("DELETE FROM work_items WHERE id = %d" % lot, "permission denied")
        self.refused("INSERT INTO agent_mailbox (sender, recipient, body)"
                     " VALUES ('orchestrateur', 'bureau', 'faux')", "mail from bureau only")
        self.refused("INSERT INTO work_item_events (work_item_id, state, note, actor)"
                     " SELECT id, 'merged', 'faux', 'bureau' FROM work_items WHERE id = %d" % lot,
                     "own external lots only")
        self.refused("INSERT INTO work_item_events (work_item_id, state, note, actor)"
                     " SELECT id, state, 'faux', 'bureau' FROM work_items WHERE id = %d" % other,
                     "own external lots only")
        self.refused("INSERT INTO provenance_receipts (subject, sha, actor, session, turn, harness)"
                     " VALUES ('commit:%s', '%s', 'orchestrateur', 's', 't', 'claude')"
                     % ("a" * 40, "a" * 40), "receipts by bureau only")
        self.refused("INSERT INTO ameesh_events (type, object, data)"
                     " VALUES ('lot', '1', '{}')", "events come from triggers only")
        self.refused("INSERT INTO session_bindings (session_id, harness, host, agent)"
                     " VALUES ('S', 'claude', '%s', 'orchestrateur')" % self.cfg.host,
                     "binds bureau")
        self.refused("INSERT INTO session_bindings (session_id, harness, host, agent)"
                     " VALUES ('S', 'claude', 'ailleurs', 'bureau')", "binds bureau")
        # courrier reçu : seules la réservation et la remise changent (hook)
        self.db.execute("INSERT INTO agent_mailbox (sender, recipient, body, meta)"
                        " VALUES ('orchestrateur', 'bureau', 'vrai', '{\"k\": 1}'),"
                        " ('orchestrateur', 'mesh-design', 'autre', '{}')")
        self.refused("UPDATE agent_mailbox SET meta = '{}' WHERE recipient = 'bureau'",
                     "reserved or delivered only")
        self.refused("UPDATE agent_mailbox SET status = 'failed' WHERE recipient = 'bureau'",
                     "reserved or delivered only")
        self.refused("UPDATE agent_mailbox SET delivered_at = now(), status = 'delivered'"
                     " WHERE recipient = 'mesh-design'", "mail from bureau only")
        self.assertEqual(self.sql("UPDATE agent_mailbox SET delivered_at = now(),"
                                  " status = 'delivered' WHERE recipient = 'bureau'").returncode, 0)
        self.refused("UPDATE agent_mailbox SET delivered_at = NULL WHERE recipient = 'bureau'",
                     "reserved or delivered only")

    def test_les_autres_connexions_ne_voient_pas_la_garde(self):
        """Le banc (superutilisateur, hors marqueur) écrit comme avant."""
        lot = self.reserve(executor="orchestrateur")
        self.db.execute("UPDATE agent_registry SET updated_at = now() WHERE name = 'bureau'")
        self.db.execute("UPDATE work_items SET state = state WHERE id = %s", (lot,))
        self.db.execute("INSERT INTO agent_mailbox (sender, recipient, body)"
                        " VALUES ('orchestrateur', 'bureau', 'vrai')")


if __name__ == "__main__":
    unittest.main()
