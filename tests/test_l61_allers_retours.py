# SPDX-License-Identifier: AGPL-3.0-only
"""L61 — commandes rapides malgré une base distante.

Chaque aller-retour vers la base coûte la latence du réseau (183 ms vers une
VM, mesuré le 2026-10-10) : ces tests bornent le nombre de connexions et
d'allers-retours des commandes les plus fréquentes, compté par le pilote
lui-même (`AMEESH_DB_TRACE`, `ameesh.db.Trace`). Une régression (une requête
par ligne, une vérification de plus) fait échouer la borne, avec la liste des
instructions envoyées dans le message d'échec.
"""
from __future__ import annotations

import json
import os
import unittest

from ameesh import db as db_mod
from ameesh import mail, registry, work

from tests.support import PgTestCase

#: bornes par commande (allers-retours, pilote psycopg). Le pilote psql en
#: fait un de plus : sa sonde de connexion, qui vérifie aussi le schéma ; et
#: chacun de ses échanges est un sous-processus, donc une connexion.
BORNES = {
    "mail list": 1,
    "list": 1,
    "work list": 2,
    "progress": 2,
    #: indépendant du nombre d'agents (avant L61 : deux requêtes par agent)
    "cost report": 3,
    #: hook sous bail, sans courrier (PostToolUse) : contrôle du bail, réservation
    "hook": 2,
    #: L521 : une lecture par section auparavant (23 allers-retours)
    "work show": 4,
    "mail whoami": 1,
    "mail inbox": 2,
    "projects": 2,
    #: L521 : 34 allers-retours avant, un `;` dans un commentaire SQL
    #: désactivait le pipeline de tout le lot
    "alerts": 6,
}


class TestAllersRetours(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.trace_path = os.path.join(self.tmp, "trace.jsonl")
        for name in ("alpha", "beta"):
            registry.upsert(self.db, name, harness="claude", host="h1", cwd="/tmp")
        self.lot = 0
        for index in range(4):
            item = work.add(self.db, title="lot %d" % index,
                            assignee="alpha" if index % 2 else "beta")
            self.lot = self.lot or item["id"]
        mail.send(self.db, "beta", "alpha", "bonjour")

    # -- outillage ---------------------------------------------------------
    def _trace(self, runner, *args, stdin=None, **extra) -> dict:
        if os.path.exists(self.trace_path):
            os.unlink(self.trace_path)
        env = self.env(AMEESH_DB_TRACE=self.trace_path, **extra)
        if stdin is None:
            proc = runner(*args, env=env)
        else:
            proc = runner(*args, env=env, stdin=stdin)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        with open(self.trace_path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(lines), 1, lines)
        trace = lines[0]
        trace["stdout"] = proc.stdout
        return trace

    def _borne(self, trace: dict, bound: int, connects: int = 1) -> None:
        # #4975 process admission is one fixed read per CLI connection, not
        # one guard per mutation. Keep the existing workload budgets intact.
        # psql traces wrap this SELECT in the JSON result envelope.
        admissions = [sql for sql in trace['statements']
                      if 'SELECT version, name, checksum FROM schema_non_additive_migrations ORDER BY version' in sql]
        self.assertEqual(len(admissions), 1, trace['statements'])
        bound += 1
        if self.db.name == "psql":
            # pilote psql : la sonde de connexion (qui vérifie aussi le
            # schéma) est un sous-processus de plus, et chaque échange est une
            # connexion
            bound += 1
            connects = bound
        detail = "\n".join("  %d. %s" % (i + 1, s) for i, s in enumerate(trace["statements"]))
        self.assertLessEqual(trace["round_trips"], bound,
                             "%d allers-retours (borne %d) :\n%s"
                             % (trace["round_trips"], bound, detail))
        self.assertLessEqual(trace["connects"], connects, detail)

    # -- commandes ---------------------------------------------------------
    def test_mail_list(self):
        trace = self._trace(self.cli, "list")
        self.assertIn("alpha", trace["stdout"])
        self._borne(trace, BORNES["mail list"])

    def test_list(self):
        trace = self._trace(self.mesh, "list")
        self.assertIn("alpha", trace["stdout"])
        self._borne(trace, BORNES["list"])

    def test_work_list(self):
        trace = self._trace(self.mesh, "work", "list")
        self.assertIn("lot 3", trace["stdout"])
        self._borne(trace, BORNES["work list"])

    def test_work_show(self):
        trace = self._trace(self.mesh, "work", "show", str(self.lot))
        self.assertIn("lot 0", trace["stdout"])
        self._borne(trace, BORNES["work show"])

    def test_mail_whoami(self):
        owner = "runner-l61-who"
        lease = registry.claim(self.db, "alpha", owner, 3600)
        self.assertIsNotNone(lease)
        env = dict(AGENT_MAIL_NAME="alpha", AMEESH_RUNNER_ID=owner,
                   AMEESH_LEASE_EPOCH=str(lease["lease_epoch"]))
        trace = self._trace(self.cli, "whoami", **env)
        self.assertIn("alpha", trace["stdout"])
        self._borne(trace, BORNES["mail whoami"])

    def test_mail_inbox(self):
        trace = self._trace(self.cli, "inbox", "alpha")
        self.assertIn("bonjour", trace["stdout"])
        self._borne(trace, BORNES["mail inbox"])

    def test_projects(self):
        trace = self._trace(self.mesh, "projects")
        self.assertIn("alpha", trace["stdout"])
        self._borne(trace, BORNES["projects"])

    def test_alerts(self):
        trace = self._trace(self.mesh, "alerts")
        self._borne(trace, BORNES["alerts"])

    def test_progress(self):
        trace = self._trace(self.mesh, "progress", "--json")
        self.assertIn("ameesh-progress/1", trace["stdout"])
        self._borne(trace, BORNES["progress"])

    def test_cost_report(self):
        for index in range(8):
            name = "agent%d" % index
            registry.upsert(self.db, name, harness="claude", host="h1", cwd="/tmp")
            os.makedirs(self.cfg.agent_dir(name), exist_ok=True)
        trace = self._trace(self.mesh, "cost", "report")
        self.assertIn("agent7", trace["stdout"])
        self._borne(trace, BORNES["cost report"])

    def test_hook_sous_bail(self):
        owner = "runner-l61"
        lease = registry.claim(self.db, "alpha", owner, 3600)
        self.assertIsNotNone(lease)
        stdin = json.dumps({"hook_event_name": "PostToolUse", "session_id": "s1",
                            "cwd": "/tmp"})
        env = dict(AGENT_MAIL_NAME="alpha", AMEESH_RUNNER_ID=owner,
                   AMEESH_LEASE_EPOCH=str(lease["lease_epoch"]))
        trace = self._trace(self.cli, "hook", "claude", stdin=stdin, **env)
        self.assertIn("bonjour", trace["stdout"])
        # première fois : + l'inscription de la session, + le solde de la remise,
        # + les trois résolutions d'alias (#423 : registre, réservation, remise)
        self._borne(trace, BORNES["hook"] + 2 + 3)
        trace = self._trace(self.cli, "hook", "claude", stdin=stdin, **env)
        self.assertEqual(trace["stdout"], "")
        # l'inscription récente n'est pas refaite (REGISTER_EVERY_S) ; les
        # résolutions d'alias de la réservation et de la remise restent
        self._borne(trace, BORNES["hook"] + 2)
        self.assertFalse(any(s.startswith("INSERT INTO agent_registry")
                             for s in trace["statements"]), trace["statements"])
        # un autre dossier : réinscrit tout de suite ; la session du hook,
        # elle, n'est jamais inscrite sous bail (L166 : celle du flux fait foi)
        autre = json.dumps({"hook_event_name": "PostToolUse", "session_id": "s2",
                            "cwd": "/ailleurs"})
        trace = self._trace(self.cli, "hook", "claude", stdin=autre, **env)
        row = registry.get(self.db, "alpha")
        self.assertEqual((row["cwd"], row["session_id"]), ("/ailleurs", None))


class TestTrace(unittest.TestCase):
    """Le compteur lui-même (sans base)."""

    def test_compte_et_remet_a_zero(self):
        trace = db_mod.Trace()
        trace.connect()
        trace.trip("SELECT  1\n FROM x")
        self.assertEqual(trace.snapshot(), {"connects": 1, "round_trips": 1,
                                            "statements": ["SELECT 1 FROM x"]})
        trace.reset()
        self.assertEqual(trace.snapshot()["round_trips"], 0)

    def test_runner_longue_duree_garde_un_historique_borne(self):
        trace = db_mod.Trace()
        for index in range(100_000):
            trace.trip("SELECT %d %s" % (index, "x" * 200))
        snapshot = trace.snapshot()
        self.assertEqual(snapshot["round_trips"], 100_000)
        self.assertEqual(len(snapshot["statements"]), 1024)
        self.assertTrue(snapshot["statements"][0].startswith("SELECT 98976 "))
        self.assertTrue(snapshot["statements"][-1].startswith("SELECT 99999 "))
        self.assertTrue(all(len(sql) <= 160 for sql in snapshot["statements"]))
        # Le format JSON consommé par le banc reste une liste, indépendante
        # du tampon ; reset conserve sa borne pour les passages suivants.
        self.assertEqual(json.loads(json.dumps(snapshot)), snapshot)
        trace.reset()
        self.assertEqual(trace.snapshot(), {"connects": 0, "round_trips": 0,
                                            "statements": []})
        self.assertEqual(len(snapshot["statements"]), 1024)
        for _ in range(2048):
            trace.trip("SELECT 1")
        self.assertEqual(len(trace.snapshot()["statements"]), 1024)


class _FausseBase:
    """Une base en mémoire : compte les échanges, rend des lignes fixes."""

    name = "fausse"
    cfg = None

    def __init__(self):
        self.echanges = []
        self.ecrit = []

    def _lignes(self, sql, params):
        if "FROM a" in sql:
            return [{"id": 1}, {"id": 2}]
        if "FROM b" in sql:
            return [{"id": i, "b": "b%d" % i} for i in params]
        return [{"n": len(self.ecrit)}]

    def query(self, sql, params=()):
        self.echanges.append([sql])
        if not db_mod.pure_read(sql):
            self.ecrit.append(sql)
        return self._lignes(sql, tuple(params))

    def query_batch(self, items):
        self.echanges.append([sql for sql, _ in items])
        return [self._lignes(sql, tuple(params)) for sql, params in items]

    def execute(self, sql, params=()):
        self.echanges.append([sql])
        self.ecrit.append(sql)
        return 1


class _BaseTableManquante(_FausseBase):
    """Une lecture optionnelle référence une table d'une migration absente :
    psycopg rend l'échec en `SchemaMissing`, la sonde de schéma ayant répondu
    oui (`_schema_ok`)."""

    _schema_ok = True

    def query(self, sql, params=()):
        if "FROM optionnelle" in sql:
            raise db_mod.SchemaMissing('relation "optionnelle" does not exist')
        return super().query(sql, params)

    def query_batch(self, items):
        if any("FROM optionnelle" in sql for sql, _ in items):
            raise db_mod.SchemaMissing('relation "optionnelle" does not exist')
        return super().query_batch(items)


class _BaseNonMigree(_FausseBase):
    """La sonde elle-même échoue : la base n'est pas migrée, rien ne doit être
    avalé par le préchargement."""

    _schema_ok = False

    def query(self, sql, params=()):
        raise db_mod.SchemaMissing("agent-mesh schema missing: run \"ameesh migrate\"")

    def query_batch(self, items):
        raise db_mod.SchemaMissing("agent-mesh schema missing: run \"ameesh migrate\"")


class TestLectureGroupee(unittest.TestCase):
    """`db.batched` : regroupement par niveaux, résultat d'une vraie exécution."""

    def test_niveaux_de_dependance(self):
        base = _FausseBase()

        def lire(db):
            ids = [r["id"] for r in db.query("SELECT id FROM a")]
            compte = db.query("SELECT count(*) AS n FROM c")
            b = db.query("SELECT id, b FROM b WHERE id IN (%s, %s)" % ("%s", "%s"), ids)
            return ids, compte, b

        ids, compte, b = db_mod.batched(base, lire)
        self.assertEqual(ids, [1, 2])
        self.assertEqual([r["b"] for r in b], ["b1", "b2"])
        # a et c ensemble, puis b (qui dépend de a) ; la vraie passe ne
        # renvoie rien à la base
        self.assertEqual([len(e) for e in base.echanges], [2, 1])

    def test_une_ecriture_coupe_le_cache(self):
        base = _FausseBase()

        def lire_ecrire(db):
            avant = db.query("SELECT count(*) AS n FROM c")
            db.execute("INSERT INTO c VALUES (1)")
            apres = db.query("SELECT count(*) AS n FROM c")
            return avant, apres

        avant, apres = db_mod.batched(base, lire_ecrire)
        self.assertEqual(base.ecrit, ["INSERT INTO c VALUES (1)"])  # une seule fois
        self.assertEqual((avant[0]["n"], apres[0]["n"]), (0, 1))

    def test_lecture_optionnelle_absente_est_ecartee(self):
        """L521 (échec CI psycopg) : une table optionnelle absente écarte sa
        lecture, elle n'arrête pas la commande — la vraie passe la rend à
        l'appelant, qui décide du repli (L160d)."""
        base = _BaseTableManquante()

        def lire(db):
            try:
                option = db.query("SELECT id FROM optionnelle")
                return db.query("SELECT id FROM a"), option, None
            except db_mod.DbError as exc:
                return db.query("SELECT id FROM a"), [], str(exc)

        ids, option, error = db_mod.batched(base, lire)
        self.assertEqual(ids, [{"id": 1}, {"id": 2}])
        self.assertEqual(option, [])
        self.assertIn("optionnelle", error or "")

    def test_base_non_migree_remonte(self):
        """La sonde dit non : `SchemaMissing` n'est pas avalée."""
        base = _BaseNonMigree()
        with self.assertRaises(db_mod.SchemaMissing):
            db_mod.batched(base, lambda db: db.query("SELECT id FROM a"))

    def test_lectures_pures(self):
        self.assertTrue(db_mod.pure_read("SELECT * FROM agent_registry WHERE updated_at > now()"))
        self.assertTrue(db_mod.pure_read("WITH x AS (SELECT 1) SELECT * FROM x"))
        for sql in ("INSERT INTO t VALUES (1) RETURNING id",
                    "WITH v AS (SELECT 1 FROM t FOR UPDATE) UPDATE t SET a = 1 RETURNING a",
                    "SELECT * FROM t FOR SHARE", "SELECT pg_advisory_xact_lock(1)",
                    "SELECT pg_notify('c', 'x')", "SELECT nextval('s')"):
            self.assertFalse(db_mod.pure_read(sql), sql)

    def test_single_statement_ignores_comments_and_literals(self):
        """L521: a `;` in a comment, a literal or a quoted identifier is not a
        statement separator — it used to disable the pipeline of a whole
        batch (`alerts`: 33 round trips instead of 3)."""
        for sql in ("SELECT 1 -- un ; dans un commentaire\n",
                    "SELECT ';' AS c",
                    'SELECT ";" FROM t',
                    "SELECT 1;  -- fin\n",
                    "/* a ; b */ SELECT 1",
                    "SELECT $$a ; b$$",
                    "SELECT 1"):
            self.assertTrue(db_mod.single_statement(sql), sql)
        for sql in ("SELECT 1; SELECT 2",
                    "SELECT 1;\nSELECT 2",
                    "SELECT 1; /* puis */ SELECT 2"):
            self.assertFalse(db_mod.single_statement(sql), sql)


class TestCacheCanon(unittest.TestCase):
    """`canon.configured_cache`: one read per command, not one per dry pass."""

    def test_read_once_inside_the_block(self):
        from types import SimpleNamespace

        from ameesh import canon as canon_mod

        calls = []

        def fake_load(root, **kwargs):
            calls.append(root)
            return SimpleNamespace(id="home", root=root)

        cfg = SimpleNamespace(canon="/tmp/canon", canon_ref="", canon_untrusted=False,
                              canon_entries=None)
        original = canon_mod.load
        canon_mod.load = fake_load
        try:
            with canon_mod.configured_cache():
                canon_mod.load_configured(cfg)
                canon_mod.load_configured(cfg)
            # outside the block, the cache is gone: each call reads again
            self.assertEqual(len(calls), 1)
            canon_mod.load_configured(cfg)
            canon_mod.load_configured(cfg)
            self.assertEqual(len(calls), 3)
        finally:
            canon_mod.load = original

    def test_seeded_window_does_not_read_again(self):
        """#4178: a caller that already holds the canons seeds the window."""
        from types import SimpleNamespace

        from ameesh import canon as canon_mod

        cfg = SimpleNamespace(canon="/tmp/canon", canon_ref="", canon_untrusted=False,
                              canon_entries=None)
        seeded = [SimpleNamespace(id="home", root="/tmp/canon")]
        original = canon_mod.load
        canon_mod.load = lambda *args, **kwargs: self.fail("seeded canon was read again")
        try:
            with canon_mod.configured_cache(cfg, seeded):
                self.assertEqual(canon_mod.load_configured(cfg), seeded)
        finally:
            canon_mod.load = original


if __name__ == "__main__":
    unittest.main()
