# SPDX-License-Identifier: AGPL-3.0-only
"""#3845 : paquet horaire déclenché par minuterie (scripts/paquet-horaire.py).

Le tic est piloté par une fausse coquille : aucune base, aucune forge. Les
témoins : rien n'est créé sans lot prêt ni pendant une réservation ; une
composition propre pose « verdict non requis » et demande la ci-full ; une
résolution laisse ce geste au pilote ; un rouge attribué à une seule unité la
retire et recompose une fois, en écrivant à son auteur ; un rouge non concluant
ne retire rien.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "paquet_horaire", ROOT / "scripts" / "paquet-horaire.py")
ph = importlib.util.module_from_spec(SPEC)
sys.modules["paquet_horaire"] = ph
SPEC.loader.exec_module(ph)

HEAD = "a" * 40
HEAD2 = "b" * 40
ZONE = ph.ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 27, 10, 25, tzinfo=ZONE).timestamp()

MANIFEST = """# Paquet

## Composition (2 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3801 | `035249f490d2` | `035249f49` | composition |
| 2 | #3807 | `6f5d44e941c4` | `6f5d44e94` | composition |
"""
RESOLVED = MANIFEST.replace("| 1 | #3801 | `035249f490d2` | `035249f49` | composition |",
                            "| 1 | #3779, #3755 | `404177ae7c83` | `51762f6fa8b1` | résolution |")


class FakeShell:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.mails: list[tuple[str, str]] = []
        self.lists: dict[str, list] = {}
        self.items: dict[int, dict] = {}
        self.ci: dict[int, dict] = {}
        self.auto_out = "manifeste reviews/x.md : tête %s\n" % HEAD
        self.auto_rc = 0

    def run(self, cmd, *, cwd=None, input_text=None, timeout=0):
        self.calls.append(list(cmd))
        if cmd[0] == "agent-mail":
            self.mails.append((cmd[2], input_text))
            return ph.Result(0)
        if cmd[0] == "git":
            if "ls-remote" in cmd:
                return ph.Result(0, "%s\trefs/heads/integration/paquet-h-20261027-03\n" % HEAD)
            return ph.Result(0)
        if cmd[0] == "ameesh":
            args = cmd[1:]
            if args[:2] == ["work", "list"]:
                state = args[args.index("--state") + 1] if "--state" in args else ""
                return ph.Result(0, json.dumps(self.lists.get(state, [])))
            if args[:2] == ["work", "add"]:
                return ph.Result(0, "lot #4000 created in intake: x\n")
            if args[:2] == ["work", "show"]:
                return ph.Result(0, json.dumps(self.items[int(args[2])]))
            if args[:2] == ["ci", "status"]:
                return ph.Result(0, json.dumps(self.ci.get(int(args[2]), {"runs": []})))
            if args[:2] == ["work", "note"]:
                item = self.items.get(int(args[2]))
                if item is not None:
                    item.setdefault("events", []).append({"note": args[3]})
            return ph.Result(0, "ok\n")
        if str(cmd[1]).endswith("paquet-auto.py"):
            return ph.Result(self.auto_rc, self.auto_out)
        raise AssertionError("commande inattendue : %r" % cmd)

    def ran(self, *prefix) -> list[list[str]]:
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)
                or (len(c) > 1 and c[1:1 + len(prefix)] == list(prefix))]


def carrier(lot=4000, *, sha=HEAD, notes=(), title="Paquet horaire h-20261027-04 — x",
            source="paquet-horaire (#3845), minuterie", created=NOW - 600):
    return {"id": lot, "app": "ameesh", "title": title, "source": source,
            "state": "qa", "branch": "integration/paquet-h-20261027-04",
            "branch_target": "main", "created_ts": created, "assignee": "mesh-design",
            "freeze": {"declared": {"sha": sha}} if sha else {},
            "events": [{"note": n} for n in notes]}


def red_run(*, attribution=True, best=(3801,), status="failure", part="psycopg 1/8"):
    test = "tests.test_x.T.test_a"
    run = {"run_id": "77", "head_sha": HEAD, "status": status, "url": "u",
           "failures": [{"test": test, "part": part, "inherited": False}]}
    if attribution:
        run["attribution"] = {"status": "ok", "tests": [{
            "test": test, "status": "probable",
            "units": [{"lot": lot, "best": True} for lot in best]
            + [{"lot": 3807, "best": False}]}]}
    return run


class Base(unittest.TestCase):
    def setUp(self):
        self.shell = FakeShell()
        self.ctx = ph.Context(repo=ROOT, target="main", project="ameesh",
                              pilot="mesh-design", actor="mesh-design",
                              shell=self.shell, python="python3", now=NOW,
                              zone=ZONE)
        self._saved = (ph.ready_lots, ph.manifest_at, ph.replay_locally)
        self.ready = [3801, 3807]
        self.manifest = MANIFEST
        self.replay = ""
        ph.ready_lots = lambda ctx: list(self.ready)
        ph.manifest_at = lambda ctx, item, sha: self.manifest
        ph.replay_locally = lambda ctx, sha, tests: self.replay

    def tearDown(self):
        ph.ready_lots, ph.manifest_at, ph.replay_locally = self._saved


class ComposeTest(Base):
    def test_rien_sans_lot_pret(self):
        self.ready = []
        self.assertIn("aucun lot prêt", ph.tick(self.ctx))
        self.assertEqual(self.shell.ran("work", "add"), [])

    def test_heure_deja_servie(self):
        self.shell.lists[""] = [dict(carrier(), state="merged", created_ts=NOW - 60)]
        self.assertIn("déjà créé", ph.tick(self.ctx))
        self.assertEqual(self.shell.ran("work", "add"), [])

    def test_reservation_par_un_autre_porteur(self):
        manual = carrier(lot=3999, title="h-33 manuel", source="mesh-design")
        self.shell.lists["qa"] = [manual]
        self.assertIn("réservée par #3999", ph.tick(self.ctx))
        self.assertEqual(self.shell.ran("work", "add"), [])
        self.assertFalse([c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)])

    def test_composition_propre_verdict_non_requis_et_ci_full(self):
        self.shell.items[4000] = carrier()
        self.assertIn("ci-full demandée", ph.tick(self.ctx))
        add = self.shell.ran("work", "add")[0]
        self.assertIn("integration/paquet-h-20261027-04", add)
        self.assertEqual(add[add.index("--title") + 1][:31], "Paquet horaire h-20261027-04 — ")
        auto = [c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)][0]
        self.assertNotIn("--force", auto)
        self.assertTrue(self.shell.ran("work", "reviewer", "4000", "--no-verdict"))
        ci = self.shell.ran("ci", "run", "4000", "--full")[0]
        self.assertEqual(ci[ci.index("--sha") + 1], HEAD)
        self.assertIn(ph.MARK_CI + HEAD, ph.marks(self.shell.items[4000]))
        self.assertEqual(self.shell.mails[-1][0], "mesh-design")

    def test_resolution_laisse_le_verdict_au_pilote(self):
        self.manifest = RESOLVED
        self.shell.items[4000] = carrier()
        ph.tick(self.ctx)
        self.assertEqual(self.shell.ran("work", "reviewer"), [])
        self.assertTrue(self.shell.ran("ci", "run", "4000", "--full"))
        self.assertIn("#1979", self.shell.mails[-1][1])

    def test_composition_vide_clot_le_porteur(self):
        self.shell.auto_out = "aucune unité composée : #3801 (conflit)\n"
        self.shell.items[4000] = carrier()
        self.assertIn("rien composé", ph.tick(self.ctx))
        self.assertTrue(self.shell.ran("work", "close", "4000", "--abandoned"))
        self.assertEqual(self.shell.ran("ci", "run"), [])


class FollowTest(Base):
    def open_carrier(self, **kw):
        item = carrier(notes=(ph.MARK_CI + HEAD,), **kw)
        self.shell.items[4000] = item
        self.shell.lists["qa"] = [item]
        return item

    def test_ci_en_cours_rien(self):
        self.open_carrier()
        self.shell.ci[4000] = {"runs": [dict(red_run(), status="running")]}
        self.assertIn("en cours", ph.tick(self.ctx))

    def test_rouge_deterministe_retire_et_recompose_une_fois(self):
        self.open_carrier()
        self.shell.items[3801] = {"id": 3801, "assignee": "mty-amh-impl-02"}
        self.shell.ci[4000] = {"runs": [red_run()]}
        ph.tick(self.ctx)
        auto = [c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)][0]
        self.assertIn("--force", auto)
        self.assertEqual(auto[auto.index("--exclude") + 1], "3801")
        self.assertIn("mty-amh-impl-02", [to for to, _ in self.shell.mails])
        notes = ph.marks(self.shell.items[4000])
        self.assertTrue(any(n.startswith(ph.MARK_RECOMPOSED) for n in notes))
        # la recomposition est republiée : ci-full sur la nouvelle tête
        self.assertTrue(self.shell.ran("ci", "run", "4000", "--full"))

    def test_rouge_apres_recomposition_ne_retire_plus(self):
        item = self.open_carrier()
        item["events"].append({"note": ph.MARK_RECOMPOSED + "1 sans #3755 (run 70)"})
        self.shell.ci[4000] = {"runs": [red_run()]}
        self.assertIn("épuisé", ph.tick(self.ctx))
        self.assertFalse([c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)])
        self.assertTrue(any(n.startswith(ph.MARK_RED + "77") for n in ph.marks(item)))
        # consigné une fois : le tic suivant ne réécrit pas au pilote
        mails = len(self.shell.mails)
        ph.tick(self.ctx)
        self.assertEqual(len(self.shell.mails), mails)

    def test_rouge_ambigu_non_concluant(self):
        self.open_carrier()
        self.shell.ci[4000] = {"runs": [red_run(best=(3801, 3807))]}
        self.assertIn("rouge consigné", ph.tick(self.ctx))
        self.assertFalse([c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)])

    def test_vert_en_local_non_concluant(self):
        self.open_carrier()
        self.replay = "vert en local (1 test(s)) : rouge non reproduit"
        self.shell.ci[4000] = {"runs": [red_run()]}
        self.assertIn("vert en local", ph.tick(self.ctx))
        self.assertFalse([c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)])


class ClassifyTest(unittest.TestCase):
    def test_cas(self):
        self.assertTrue(ph.classify(red_run()).deterministic)
        self.assertEqual(ph.classify(red_run()).lots, [3801])
        self.assertFalse(ph.classify(red_run(status="cancelled")).deterministic)
        self.assertFalse(ph.classify(red_run(part="job")).deterministic)
        self.assertFalse(ph.classify(red_run(attribution=False)).deterministic)
        inherited = red_run()
        inherited["failures"][0]["inherited"] = True
        self.assertFalse(ph.classify(inherited).deterministic)

    def test_unites_du_manifeste(self):
        units = ph.composition_units(RESOLVED)
        self.assertEqual([u.resolution for u in units], [True, False])
        self.assertEqual(units[0].lots, [3779, 3755])

    def test_heure_locale(self):
        start = ph.hour_start(NOW, ZONE)
        self.assertEqual(datetime.fromtimestamp(start, ZONE).minute, 0)
        self.assertLessEqual(NOW - start, 3600)


class ExcludeOptionTest(unittest.TestCase):
    def test_paquet_auto_accepte_exclude(self):
        parsed = []
        original = ph.auto.compose_packet
        ph.auto.compose_packet = lambda args: parsed.append(args.exclude) or 0
        try:
            rc = ph.auto.main(["--name", "h-x", "--carrier", "9",
                               "--exclude", "3801", "--exclude", "3755"])
        finally:
            ph.auto.compose_packet = original
        self.assertEqual(rc, 0)
        self.assertEqual(parsed, [[3801, 3755]])


if __name__ == "__main__":
    unittest.main()
