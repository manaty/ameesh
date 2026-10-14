# SPDX-License-Identifier: AGPL-3.0-only
"""#1343 — la décision de `ameesh ci run` / `ci cancel` : gel, avis, idempotence.

Le canon §3 (owner 14/10 21:45) met la relecture avant la CI : une poussée de
lot ne lance plus rien, et la CI du lot (impactés + contrat) part au gel, une
fois, sur la tête gelée. Ces témoins tiennent la décision seule
(`ci.gel_plan`, `ci.cancel_plan`, le dispatch de `ci.cmd_ci_run`), sans réseau
ni base : le faux `api` rend les quatre lectures GitHub (tête de branche,
exécutions, merge-base, contenu du workflow), les jalons et le journal du lot
sont posés à la main, et la transaction du faux `db` prend un verrou — deux
`ci run` concurrents se sérialisent comme sur la ligne `work_items`.

Chaque refus est vérifié par son message *et* par l'absence de lecture de la
base ou du workflow — donc aucun dispatch possible ; chaque acceptation prouve
que c'est le SHA gelé qui part, avec sa base. Le faux `head_sha=`/`branch=`
applique le filtre de la requête comme GitHub le ferait : un témoin ne passe
pas parce que le faux aurait rendu des exécutions que la vraie requête
n'aurait pas rendues.

Les quatre correctifs de la R1 Codex sont tenus ici : P1-1 le dernier verdict
d'un relecteur annule son GO ancien (`blocked` récent), P1-3 le mode d'une
exécution compte (un vert `lot` ne vaut pas un `--full` vert), P2-4 `--force`
ne rejoue ni un vert ni un run en vol, P2-5 un dispatch déjà réservé recule et
le verrou de la ligne du lot sérialise deux `ci run`.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime
import io
import itertools
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ameesh import ci, mesh_cli, plan_github, work  # noqa: E402

DEPOT = "manaty/ameesh"
GEL = "a" * 40
TETE = "b" * 40
BASE = "c" * 40
AUTRE = "d" * 40
BRANCHE = "lot/1343-ci-au-gel"


def lot(**extra) -> dict:
    """Le lot relu : une branche de lot, une cible main, un assigné."""
    row = {"id": 1343, "app": "ameesh", "assignee": "mty-amh-impl-04",
           "branch": BRANCHE, "branch_target": "main",
           "issue_ref": "manaty/ameesh#1040"}
    row.update(extra)
    return row


def jalon(kind: str, *, sha: str = "", actor: str = "",
          verdict: str | None = None) -> dict:
    """Un jalon tel que `work.milestones` le rend (les clés lues suffisent)."""
    return {"kind": kind, "sha": sha, "actor": actor, "verdict": verdict}


def execution(ident: int = 1, *, statut: str = "completed",
              conclusion: str | None = "success", sha: str = GEL,
              branche: str = BRANCHE, mode: str = "lot",
              event: str = "workflow_dispatch", titre: str | None = None,
              base: str | None = None, created: float | None = None) -> dict:
    """Une exécution de `tests.yml` telle que GitHub la rend.

    Le titre porte le mode d'un dispatch (#1343) ; `titre` force un ancien run
    sans mode lisible, `base` la cible d'une PR, `created` l'instant de
    création (epoch) qui date la matérialisation d'une réservation.
    """
    if titre is None:
        titre = ("tests %s %s lot #1343" % (mode, sha)
                 if event == "workflow_dispatch" else "un commit")
    row = {"id": ident, "status": statut, "conclusion": conclusion, "head_sha": sha,
           "head_branch": branche, "event": event, "display_title": titre, "name": "tests",
           "html_url": "https://github.com/%s/actions/runs/%d" % (DEPOT, ident)}
    if created is not None:
        row["created_at"] = datetime.datetime.fromtimestamp(
            created, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    if base:
        row["pull_requests"] = [{"base": {"ref": base}}]
    return row


def marque(state: str, *, sha: str = GEL, mode: str = "lot", token: str = "0" * 16,
           ident: int = 1, ts: float | None = None, texte: str = "") -> dict:
    """Un événement `ci-dispatch` du journal du lot, comme `work.events` le rend."""
    note = "ci-dispatch %s sha=%s mode=%s token=%s" % (state, sha, mode, token)
    if texte:
        note += ": " + texte
    return {"id": ident, "note": note, "created_ts": time.time() if ts is None else ts}


class FakeTx:
    """La transaction du faux `db` : comptée, et ouverte tant qu'on est dedans."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.transactions += 1
        self.db.tx_open = True
        return self

    def __exit__(self, *exc):
        self.db.tx_open = False
        return False


class FakeDb:
    """La connexion : une transaction qui n'écrit rien (les `work` sont faux)."""

    def __init__(self):
        self.transactions = 0
        self.tx_open = False

    def transaction(self):
        return FakeTx(self)


class Github:
    """Les lectures GitHub de `gel_plan`/`cancel_plan`/`cmd_ci_run`, sans réseau."""

    def __init__(self, *, head: str = GEL, runs=(), merge_base: str = BASE,
                 workflow: str | None = "#1343 : declencheur du gel\n"):
        self.head = head
        self.runs = list(runs)
        self.merge_base = merge_base
        self.workflow = workflow
        self.paths: list[str] = []

    def rows(self) -> list[dict]:
        """Les exécutions que la liste rend (une sous-classe peut en ajouter)."""
        return list(self.runs)

    def __call__(self, path: str) -> dict:
        self.paths.append(path)
        if path.startswith("repos/%s/branches/" % DEPOT):
            return {"commit": {"sha": self.head}}
        if "/actions/workflows/tests.yml/runs?" in path:
            runs = self.rows()
            found = re.search(r"[?&]head_sha=([0-9a-f]{40})", path)
            if found:
                runs = [r for r in runs if (r.get("head_sha") or "") == found.group(1)]
            found = re.search(r"[?&]branch=([^&]+)", path)
            if found:
                branche = unquote(found.group(1))
                runs = [r for r in runs if (r.get("head_branch") or "") == branche]
            return {"workflow_runs": runs}
        if path.startswith("repos/%s/compare/" % DEPOT):
            return {"merge_base_commit": {"sha": self.merge_base}}
        if "/contents/.github/workflows/tests.yml" in path:
            if self.workflow is None:
                return {}
            return {"content": base64.b64encode(self.workflow.encode()).decode()}
        raise AssertionError("chemin GitHub inattendu : %s" % path)

    def ecrit(self) -> list[str]:
        """Les lectures faites seulement avant un dispatch (base, workflow)."""
        return [p for p in self.paths if "/compare/" in p or "/contents/" in p]


class Gh:
    """Le `gh` des écritures : enregistre les argv, réussit (sauf refus)."""

    def __init__(self, refuse_dispatch: bool = False):
        self.calls: list[list[str]] = []
        self.refuse_dispatch = refuse_dispatch

    def run(self, argv: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if self.refuse_dispatch and argv and argv[0] == "workflow":
            return 1, "", "Unexpected inputs provided: ['exception']"
        return 0, "", ""

    def annulations(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["api", "-X"]]

    def dispatch(self) -> list[str]:
        return next((c for c in self.calls if c and c[0] == "workflow"), [])


class RecordingGh(Gh):
    """Le `gh` qui retient si le dispatch s'appelle transaction ouverte."""

    def __init__(self, db, **kw):
        super().__init__(**kw)
        self.db = db
        self.open_dispatches = 0

    def run(self, argv, stdin=None):
        if argv and argv[0] == "workflow" and self.db.tx_open:
            self.open_dispatches += 1
        return super().run(argv, stdin)


class GelTestCase(unittest.TestCase):
    """Le faux `work` : un lot, ses jalons et son journal, sans base ni courrier."""

    def setUp(self) -> None:
        self.item = lot()
        self.jalons: list[dict] = []
        self.events: list[dict] = []
        self.db = FakeDb()
        self.work = contextlib.ExitStack()
        self.addCleanup(self.work.close)
        self.work.enter_context(mock.patch.object(work, "get", self._get))
        self.work.enter_context(mock.patch.object(work, "milestones", self._milestones))
        self.work.enter_context(mock.patch.object(work, "events", self._events))

    def _get(self, db, item_id):
        return self.item if int(item_id) == self.item["id"] else None

    def _milestones(self, db, item_id, limit=100):
        # `work.milestones` rend du plus récent au plus ancien : le faux suit
        # l'ordre de la requête, pas l'ordre d'insertion.
        return list(reversed(self.jalons))

    def _events(self, db, item_id, limit=100):
        return list(reversed(self.events))

    def gele(self, sha: str = GEL, acteur: str = "relecteur") -> None:
        """Le lot gelé sur `sha`, avec un verdict `ok` indépendant dessus."""
        self.jalons += [jalon("frozen", sha=sha, actor="auteur"),
                        jalon("verdict", sha=sha, actor=acteur, verdict="ok")]

    def gel(self, api, **kw) -> dict:
        args = {"sha": "", "avis": 1, "exception": "", "reason": "", "full": False,
                "force": False, "repo": DEPOT}
        args.update(kw)
        return ci.gel_plan(self.db, None, self.item["id"], api=api, **args)

    def annule(self, api, **kw) -> dict:
        args = {"all_runs": False, "repo": DEPOT}
        args.update(kw)
        return ci.cancel_plan(self.db, None, self.item["id"], api=api, **args)


class GelPlanTest(GelTestCase):
    """`ci.gel_plan` : ce qui refuse, ce qui ne redispatch pas, ce qui part."""

    def test_un_lot_sans_gel_est_refuse_avant_toute_lecture(self):
        api = Github()
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api)
        self.assertIn("is not frozen", str(caught.exception))
        self.assertEqual(api.paths, [], "rien ne doit être lu sans gel")

    def test_une_tete_bougee_est_refusee_jamais_un_vert_ailleurs(self):
        self.gele()
        api = Github(head=TETE)
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api)
        self.assertIn("the ref moved", str(caught.exception))
        self.assertEqual(api.ecrit(), [], "aucune base, aucun workflow lu")

    def test_un_avis_independant_dispatch_sur_le_sha_gele(self):
        self.gele()
        api = Github()
        decision = self.gel(api)
        self.assertEqual(decision["action"], "dispatch")
        self.assertEqual(decision["sha"], GEL)
        self.assertEqual(decision["base"], BASE)
        self.assertEqual(decision["mode"], "lot")
        self.assertEqual(decision["verdicts"], ["relecteur"])
        self.assertIn("compare/main...%s" % GEL, " ".join(api.paths))

    def test_l_avis_de_l_auteur_ne_compte_pas(self):
        self.jalons += [jalon("frozen", sha=GEL),
                        jalon("verdict", sha=GEL, actor=self.item["assignee"], verdict="ok")]
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github())
        self.assertIn("0 independent verdict(s)", str(caught.exception))

    def test_deux_avis_du_meme_acteur_ne_font_qu_un(self):
        self.jalons += [jalon("frozen", sha=GEL),
                        jalon("verdict", sha=GEL, actor="relecteur", verdict="ok"),
                        jalon("verdict", sha=GEL, actor="relecteur", verdict="ok")]
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github(), avis=2)
        self.assertIn("1 independent verdict(s)", str(caught.exception))
        self.jalons.append(jalon("verdict", sha=GEL, actor="second", verdict="ok"))
        self.assertEqual(self.gel(Github(), avis=2)["action"], "dispatch")

    def test_un_avis_sur_une_autre_tete_ne_compte_pas(self):
        self.jalons += [jalon("frozen", sha=GEL),
                        jalon("verdict", sha=TETE, actor="relecteur", verdict="ok")]
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github())
        self.assertIn("0 independent verdict(s)", str(caught.exception))

    def test_un_verdict_bloque_ne_compte_pas(self):
        self.jalons += [jalon("frozen", sha=GEL),
                        jalon("verdict", sha=GEL, actor="relecteur", verdict="blocked")]
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github())
        self.assertIn("blocked", str(caught.exception))

    def test_un_blocked_recent_annule_un_go_ancien_du_meme_acteur(self):
        # P1 R1 : le dernier mot de chaque relecteur vaut ; un GO d'avant le
        # blocked ne se recycle pas (même acteur, même SHA).
        self.jalons += [jalon("frozen", sha=GEL),
                        jalon("verdict", sha=GEL, actor="relecteur", verdict="ok"),
                        jalon("verdict", sha=GEL, actor="relecteur", verdict="blocked")]
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github())
        self.assertIn("relecteur", str(caught.exception))
        self.assertIn("blocked", str(caught.exception))

    def test_un_blocked_puis_ok_du_meme_acteur_compte(self):
        # le dernier verdict du relecteur est `ok` : son blocked ancien est levé
        # par sa propre relecture, sur la même tête.
        self.jalons += [jalon("frozen", sha=GEL),
                        jalon("verdict", sha=GEL, actor="relecteur", verdict="blocked"),
                        jalon("verdict", sha=GEL, actor="relecteur", verdict="ok")]
        self.assertEqual(self.gel(Github())["action"], "dispatch")

    def test_un_blocked_d_un_autre_relecteur_bloque_la_tete(self):
        # P1 R1 : la tête refusée par un relecteur ne part pas, même si un
        # autre a dit `ok` — une correction demande une nouvelle tête.
        self.jalons += [jalon("frozen", sha=GEL),
                        jalon("verdict", sha=GEL, actor="r1", verdict="ok"),
                        jalon("verdict", sha=GEL, actor="r2", verdict="blocked")]
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github(), avis=1)
        self.assertIn("r2", str(caught.exception))

    def test_un_lot_leger_dispatch_sans_avis(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        self.assertEqual(self.gel(Github(), avis=0)["action"], "dispatch")

    def test_un_vert_deja_obtenu_ne_redispatch_pas(self):
        self.gele()
        api = Github(runs=[execution(7, conclusion="success")])
        decision = self.gel(api)
        self.assertEqual((decision["action"], decision["run"]["id"]), ("green", 7))
        self.assertEqual(api.ecrit(), [], "un vert ne relit ni base ni workflow")

    def test_un_vert_de_mode_lot_ne_fait_pas_un_full_deja_vert(self):
        # P1 R1 : un run `lot` vert ne prouve pas la suite complète qu'un
        # `--full` demande ; le dispatch part.
        self.jalons.append(jalon("frozen", sha=GEL))
        api = Github(runs=[execution(7, mode="lot")])
        decision = self.gel(api, full=True, reason="paquet §12")
        self.assertEqual((decision["action"], decision["mode"]), ("dispatch", "full"))

    def test_un_vert_full_couvre_un_dispatch_lot(self):
        # la suite complète inclut les impactés et le contrat : elle couvre le
        # lot, jamais l'inverse.
        self.gele()
        api = Github(runs=[execution(7, mode="full")])
        decision = self.gel(api)
        self.assertEqual((decision["action"], decision["run"]["id"]), ("green", 7))

    def test_un_run_sans_mode_lisible_ne_verdit_pas(self):
        # un ancien dispatch (titre « tests ») ne prouve aucun mode : le doute
        # ne verdit pas un check requis.
        self.jalons.append(jalon("frozen", sha=GEL))
        api = Github(runs=[execution(7, titre="tests")])
        self.assertEqual(self.gel(api, full=True, reason="release")["action"], "dispatch")

    def test_une_pr_de_lot_d_avant_1343_ne_verdit_pas(self):
        # un run de PR vers main (d'avant #1343) n'est pas la CI du gel.
        self.gele()
        api = Github(runs=[execution(7, event="pull_request", base="main")])
        self.assertEqual(self.gel(api)["action"], "dispatch")

    def test_une_execution_en_cours_ne_redispatch_pas(self):
        self.gele()
        api = Github(runs=[execution(8, statut="in_progress", conclusion=None)])
        self.assertEqual(self.gel(api)["action"], "running")

    def test_un_rouge_demande_force(self):
        self.gele()
        rouge = [execution(9, conclusion="failure")]
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github(runs=rouge))
        self.assertIn("--force", str(caught.exception))
        self.assertEqual(self.gel(Github(runs=rouge), force=True)["action"], "dispatch")

    def test_force_ne_rejoue_pas_un_vert(self):
        # P2 R1 : `--force` ne lève pas l'idempotence d'un vert.
        self.gele()
        api = Github(runs=[execution(7, conclusion="success")])
        decision = self.gel(api, force=True)
        self.assertEqual((decision["action"], decision["run"]["id"]), ("green", 7))

    def test_force_ne_rejoue_pas_un_run_en_vol(self):
        # P2 R1 : ni celle d'un run en cours.
        self.gele()
        api = Github(runs=[execution(8, statut="queued", conclusion=None)])
        self.assertEqual(self.gel(api, force=True)["action"], "running")

    def test_une_reservation_fraiche_ne_redispatch_pas(self):
        # P2 R1 : un dispatch parti mais pas encore visible dans l'API tient la
        # clé ; GitHub ne le montre qu'avec un délai.
        self.gele()
        self.events.append(marque("dispatched", token="1" * 16))
        api = Github()
        decision = self.gel(api)
        self.assertEqual(decision["action"], "running")
        self.assertIn("reserved", decision["why"])
        self.assertEqual(api.ecrit(), [], "une réservation ne relit ni base ni workflow")

    def test_une_reservation_perimee_laisse_redispatch(self):
        self.gele()
        self.events.append(marque("dispatched", token="1" * 16, ts=0.0))
        self.assertEqual(self.gel(Github())["action"], "dispatch")

    def test_un_dispatch_refuse_ne_tient_pas_la_cle(self):
        # `released` referme la clé : un refus de GitHub ne bloque pas l'essai
        # suivant.
        self.gele()
        self.events.append(marque("released", token="1" * 16, texte="dispatch refused"))
        self.assertEqual(self.gel(Github())["action"], "dispatch")

    def test_un_sha_demande_hors_gel_est_refuse(self):
        self.gele()
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github(), sha=AUTRE)
        self.assertIn("is not the freeze", str(caught.exception))

    def test_un_sha_seul_ne_remplace_pas_le_gel(self):
        # le gel est un jalon : nommer un SHA ne dispense pas de le déclarer,
        # sinon la CI d'un lot jamais gelé partirait sans trace de gel.
        api = Github()
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api, sha=GEL)
        self.assertIn("is not frozen", str(caught.exception))
        self.assertEqual(api.paths, [], "rien ne doit être lu sans gel")

    def test_une_exception_apres_gel_test_la_tete_du_correctif(self):
        # un correctif d'incident arrive après le gel : l'exception teste la
        # tête, pas le gel périmé, et le dispatch est marqué.
        self.gele()
        api = Github(head=TETE)
        decision = self.gel(api, exception="incident", reason="incident prod")
        self.assertEqual((decision["action"], decision["sha"]), ("dispatch", TETE))
        self.assertEqual(decision["base"], BASE)

    def test_un_workflow_sans_declencheur_est_refuse(self):
        self.gele()
        api = Github(workflow="name: tests\n")
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api)
        self.assertIn("does not carry the freeze trigger", str(caught.exception))

    def test_une_base_introuvable_est_refusee(self):
        self.gele()
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github(merge_base=""))
        self.assertIn("merge-base", str(caught.exception))

    def test_une_exception_dispatch_sans_gel_ni_avis_mais_marquee(self):
        api = Github(head=TETE)
        decision = self.gel(api, exception="incident", reason="CI cassée sur main")
        self.assertEqual(decision["action"], "dispatch")
        self.assertEqual((decision["sha"], decision["exception"]), (TETE, "incident"))
        self.assertIn("CI cassée sur main", decision["reason"])

    def test_une_exception_sans_reason_est_refusee(self):
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github(), exception="unblock")
        self.assertIn("requires --reason", str(caught.exception))
        with self.assertRaises(ci.GelError):
            self.gel(Github(), exception="autre", reason="x")

    def test_full_dispatch_sans_avis_en_mode_full(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        decision = self.gel(Github(), full=True, reason="paquet §12")
        self.assertEqual((decision["action"], decision["mode"]), ("dispatch", "full"))

    def test_avis_hors_bornes_refuse(self):
        for avis in (-1, 3):
            with self.assertRaises(ci.GelError):
                self.gel(Github(), avis=avis)

    def test_lot_inconnu_refuse(self):
        with self.assertRaises(ci.GelError) as caught:
            ci.gel_plan(self.db, None, 999, api=Github(), repo=DEPOT)
        self.assertIn("unknown", str(caught.exception))

    def test_lot_sans_branche_refuse(self):
        self.item = lot(branch="")
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github())
        self.assertIn("has no branch", str(caught.exception))


class CancelPlanTest(GelTestCase):
    """`ci.cancel_plan` : les exécutions non conclues, le SHA gelé gardé."""

    def test_seuls_les_runs_ouverts_hors_sha_gele_sont_annules(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        api = Github(runs=[execution(1, statut="in_progress", conclusion=None, sha=GEL),
                           execution(2, statut="queued", conclusion=None, sha=TETE),
                           execution(3, conclusion="success", sha=TETE)])
        report = self.annule(api)
        self.assertEqual([r["id"] for r in report["runs"]], [2])
        self.assertIn(GEL[:12], report["why"])

    def test_le_sha_gele_s_annule_avec_all(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        api = Github(runs=[execution(1, statut="in_progress", conclusion=None, sha=GEL),
                           execution(2, statut="queued", conclusion=None, sha=TETE)])
        report = self.annule(api, all_runs=True)
        self.assertEqual(sorted(r["id"] for r in report["runs"]), [1, 2])
        self.assertTrue(report["all"])

    def test_un_lot_sans_gel_annule_tous_ses_runs_ouverts(self):
        api = Github(runs=[execution(1, statut="queued", conclusion=None, sha=GEL),
                           execution(2, conclusion="failure", sha=TETE)])
        self.assertEqual([r["id"] for r in self.annule(api)["runs"]], [1])

    def test_un_lot_sans_branche_refuse(self):
        self.item = lot(branch="")
        with self.assertRaises(ci.GelError) as caught:
            self.annule(Github())
        self.assertIn("nothing to cancel", str(caught.exception))


class DispatchTest(GelTestCase):
    """`ci.cmd_ci_run` : annule les périmés, dispatche une fois, note le lot."""

    def setUp(self) -> None:
        super().setUp()
        self.args = argparse.Namespace(id=1343, sha="", avis=1, exception="", reason="",
                                       full=False, force=False, repo=DEPOT, dry_run=False,
                                       json=True)
        self.notes: list[str] = []
        self.seq = itertools.count(1)
        self.on_note = None
        self.work.enter_context(mock.patch.object(work, "note", self._note))

    def _note(self, db, item_id, text, **kw):
        """La note du faux `work` : journalisée, et lisible par `work.events`."""
        self.notes.append(text)
        self.events.append({"id": next(self.seq), "note": text,
                            "created_ts": time.time()})
        if self.on_note and text.startswith("ci-dispatch reserve"):
            self.on_note(text)
        return True

    def dispatch(self, api, gh, *, args=None):
        """`cmd_ci_run` avec le `gh`, la base et la note remplacés."""
        with mock.patch("ameesh.db.open_db", lambda cfg: self.db), \
                mock.patch.object(plan_github, "Gh", lambda *a, **k: gh), \
                mock.patch.object(plan_github, "_json",
                                  lambda g, argv, *a, **k: api(argv[-1])), \
                contextlib.redirect_stdout(io.StringIO()):
            code = ci.cmd_ci_run(None, args or self.args)
        return code, self.notes

    def marques(self, state: str) -> list[str]:
        return [n for n in self.notes if n.startswith("ci-dispatch %s" % state)]

    def test_dispatch_annule_les_perimes_puis_porte_sha_base_lot(self):
        self.gele()
        api = Github(runs=[execution(4, statut="queued", conclusion=None, sha=TETE)])
        gh = Gh()
        code, notes = self.dispatch(api, gh)
        self.assertEqual(code, 0)
        self.assertEqual([c[-1] for c in gh.annulations()],
                         ["repos/%s/actions/runs/4/cancel" % DEPOT])
        self.assertEqual(gh.dispatch(),
                         ["workflow", "run", "tests.yml", "--repo", DEPOT,
                          "--ref", BRANCHE, "-f", "mode=lot", "-f", "sha=%s" % GEL,
                          "-f", "base=%s" % BASE, "-f", "lot=1343"])
        dispatched = self.marques("dispatched")
        self.assertEqual(len(dispatched), 1)
        self.assertIn("dispatched on %s" % GEL, dispatched[0])
        self.assertEqual(len(self.marques("reserve")), 1, notes)

    def test_dry_run_n_annule_ni_ne_dispatche(self):
        self.gele()
        api = Github(runs=[execution(4, statut="queued", conclusion=None, sha=TETE)])
        gh = Gh()
        code, notes = self.dispatch(api, gh, args=argparse.Namespace(
            **{**vars(self.args), "dry_run": True}))
        self.assertEqual((code, gh.calls, notes), (0, [], []))

    def test_un_dispatch_refuse_est_dit_et_note(self):
        self.gele()
        api = Github()
        gh = Gh(refuse_dispatch=True)
        code, notes = self.dispatch(api, gh)
        self.assertEqual(code, 1)
        released = self.marques("released")
        self.assertEqual(len(released), 1)
        self.assertIn("dispatch refused", released[0])
        self.assertIn("Unexpected inputs", released[0])
        self.assertEqual(self.marques("dispatched"), [])

    def test_une_exception_nomme_le_drapeau_dans_le_dispatch(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        api = Github()
        gh = Gh()
        code, notes = self.dispatch(api, gh, args=argparse.Namespace(
            **{**vars(self.args), "exception": "unblock", "reason": "main rouge"}))
        self.assertEqual(code, 0)
        self.assertIn("exception=unblock", gh.dispatch())
        self.assertNotIn("main rouge", " ".join(gh.dispatch()),
                         "le motif reste au lot, pas dans l'argv du dispatch")
        self.assertTrue(any("exception unblock: main rouge" in n for n in notes))

    def test_un_dispatch_deja_reserve_recule(self):
        # P2 R1 : la marque fraîche d'un autre fait reculer le second dès la
        # décision — aucun `gh workflow run`, aucune note.
        self.gele()
        self.events.append(marque("dispatched", token="1" * 16))
        gh = Gh()
        code, notes = self.dispatch(Github(), gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.dispatch(), [])
        self.assertEqual(gh.calls, [], "aucun geste gh, pas même une annulation")
        self.assertEqual(notes, [])

    def test_un_run_qui_apparait_sous_le_verrou_recule(self):
        # P2 R1 : la course inter-hôtes — la décision a vu la place vide, mais
        # l'exécution d'un autre dispatch devient visible pendant l'attente du
        # verrou ; la relecture sous le verrou recule.
        self.gele()
        api = Github()
        self.on_note = lambda text: api.runs.append(
            execution(11, statut="queued", conclusion=None))
        gh = Gh()
        code, notes = self.dispatch(api, gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.dispatch(), [])
        self.assertEqual(self.marques("dispatched"), [])
        self.assertEqual(len(self.marques("reserve")), 1)

    def test_une_marque_concurrente_sous_le_verrou_recule(self):
        # P2 R1 : un autre hôte a commité sa marque entre la décision et le
        # verrou ; le second la lit et recule, en refermant la sienne.
        self.gele()
        self.on_note = lambda text: self.events.append(
            marque("dispatched", token="2" * 16, ident=99))
        gh = Gh()
        code, notes = self.dispatch(Github(), gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.dispatch(), [])
        self.assertEqual(self.marques("dispatched"), [])
        self.assertEqual(len(self.marques("released")), 1)

    def test_le_dispatch_s_appelle_dans_la_transaction_du_lot(self):
        # P2 R1 : le dispatch tient le verrou de la ligne `work_items` (pris par
        # la note `reserve`) pendant tout l'appel `gh workflow run` ; deux
        # `ci run` concurrents, même d'hôtes différents, se sérialisent.
        self.gele()
        gh = RecordingGh(self.db)
        code, notes = self.dispatch(Github(), gh)
        self.assertEqual(code, 0)
        self.assertEqual(self.db.transactions, 1)
        self.assertEqual(gh.open_dispatches, 1,
                         "le gh du dispatch s'appelle transaction ouverte")
        self.assertFalse(self.db.tx_open, "la transaction est refermée")


class ParserTest(unittest.TestCase):
    """Le parseur : `ci run`/`ci cancel` et leurs défauts, sans base."""

    def test_ci_run_et_cancel_sont_du_parseur(self):
        parser = mesh_cli.build_parser()
        args = parser.parse_args(["ci", "run", "1343"])
        self.assertEqual(args.func.__name__, "cmd_ci")
        self.assertEqual((args.id, args.avis), (1343, 1))
        self.assertFalse(args.full or args.force or args.dry_run)
        args = parser.parse_args(["ci", "cancel", "1343", "--all", "--dry-run"])
        self.assertEqual(args.func.__name__, "cmd_ci")
        self.assertTrue(args.all and args.dry_run)


if __name__ == "__main__":
    unittest.main()
