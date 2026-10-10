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
}


class TestAllersRetours(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.trace_path = os.path.join(self.tmp, "trace.jsonl")
        for name in ("alpha", "beta"):
            registry.upsert(self.db, name, harness="claude", host="h1", cwd="/tmp")
        for index in range(4):
            work.add(self.db, title="lot %d" % index, assignee="alpha" if index % 2 else "beta")
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
        # première fois : + l'inscription de la session, + le solde de la remise
        self._borne(trace, BORNES["hook"] + 2)
        trace = self._trace(self.cli, "hook", "claude", stdin=stdin, **env)
        self.assertEqual(trace["stdout"], "")
        # l'inscription récente n'est pas refaite (REGISTER_EVERY_S)
        self._borne(trace, BORNES["hook"])
        self.assertFalse(any(s.startswith("INSERT INTO agent_registry")
                             for s in trace["statements"]), trace["statements"])
        # une autre session : réinscrite tout de suite
        autre = json.dumps({"hook_event_name": "PostToolUse", "session_id": "s2",
                            "cwd": "/tmp"})
        trace = self._trace(self.cli, "hook", "claude", stdin=autre, **env)
        self.assertEqual(registry.get(self.db, "alpha")["session_id"], "s2")


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

    def test_lectures_pures(self):
        self.assertTrue(db_mod.pure_read("SELECT * FROM agent_registry WHERE updated_at > now()"))
        self.assertTrue(db_mod.pure_read("WITH x AS (SELECT 1) SELECT * FROM x"))
        for sql in ("INSERT INTO t VALUES (1) RETURNING id",
                    "WITH v AS (SELECT 1 FROM t FOR UPDATE) UPDATE t SET a = 1 RETURNING a",
                    "SELECT * FROM t FOR SHARE", "SELECT pg_advisory_xact_lock(1)",
                    "SELECT pg_notify('c', 'x')", "SELECT nextval('s')"):
            self.assertFalse(db_mod.pure_read(sql), sql)


if __name__ == "__main__":
    unittest.main()
