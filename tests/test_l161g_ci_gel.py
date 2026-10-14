# SPDX-License-Identifier: AGPL-3.0-only
"""#1343 — la décision de `ameesh ci run` / `ci cancel` : gel, avis, étiquette.

Le canon §3 (owner 14/10 21:45) met la relecture avant la CI : une poussée de
lot ne lance plus rien, et la CI du lot (impactés + contrat) part au gel, une
fois, sur la tête gelée. Le déclencheur retenu est l'étiquette
`ci-lot-<40hex>` / `ci-full-<40hex>` posée sur la PR (`pull_request`
`types: [labeled]`) : un `workflow_dispatch` ne satisfait pas un check requis
de PR. Ces témoins tiennent la décision seule (`ci.gel_plan`, `ci.cancel_plan`,
la pose de `ci.cmd_ci_run`), sans réseau ni base : le faux `api` rend les cinq
lectures GitHub (tête de branche, exécutions, PR, contenu du workflow, check
requis), les jalons et le journal du lot sont posés à la main, et la
transaction du faux `db` prend un verrou — deux `ci run` concurrents se
sérialisent comme sur la ligne `work_items`.

Chaque refus est vérifié par son message *et* par l'absence de lecture du
workflow ou de pose d'étiquette — donc aucun run ne peut partir ; chaque
acceptation prouve que c'est le SHA gelé qui part, avec sa base et son mode.
Le faux `head_sha=`/`branch=`/`event=` applique le filtre de la requête comme
GitHub le ferait : un témoin ne passe pas parce que le faux aurait rendu des
exécutions que la vraie requête n'aurait pas rendues.

Les correctifs de la R1 Codex sont tenus ici : P1-1 le dernier verdict d'un
relecteur annule son GO ancien (`blocked` récent), P1-2 un push ou un ancien
dispatch vert ne remplace pas le check de la PR (parcours étiqueté seul),
P1-3 le mode et la composition d'une exécution comptent (même tête mais autre
base, ou `lot` pour un `--full`, ne verdissent pas), P2-4 `--force` ne rejoue
ni un vert applicable ni un run en vol, P2-5 une pose déjà réservée recule et
le verrou de la ligne du lot sérialise deux `ci run`.

L'avis delta R1 sur `3ce572f7b` est tenu aussi : le gel et les derniers
verdicts sont relus sous le verrou (`ci._revalidate`) — un `blocked` ou un
nouveau gel commité pendant l'attente fait reculer la pose, les exceptions
explicites (`--exception`, `--full`) gardant leur cours ; la phase critique
refait la décision check/mode/`--force` du plan (le `--force` d'un check requis
rouge part) ; la marque d'un run rouge visible ne tient plus la clé (`after`) ;
une réservation `full` couvre une demande `lot`, jamais l'inverse ; le chemin
médié, sans transaction, reste une réservation de meilleur effort — la
sérialisation par verrou de ligne est bornée aux connexions directes.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime
import io
import itertools
import json
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
PR = 1047
LABEL = "ci-lot-%s" % GEL
LABEL_FULL = "ci-full-%s" % GEL
WORKFLOW = ("name: tests\n# #1343\non:\n  pull_request:\n    types: [labeled]\n"
            "# ci-(lot|full)-<sha>\n")


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


def pull(*, number: int = PR, head: str = GEL, base: str = BASE,
         head_ref: str = BRANCHE, base_ref: str = "main", labels=()) -> dict:
    """Une PR ouverte telle que `repos/R/pulls` la rend."""
    return {"number": number,
            "head": {"sha": head, "ref": head_ref},
            "base": {"sha": base, "ref": base_ref},
            "labels": [{"name": name} for name in labels]}


def label_run(ident: int = 7, *, sha: str = GEL, base: str = BASE, mode: str = "lot",
              statut: str = "completed", conclusion: str | None = "success",
              number: int = PR, event: str = "pull_request", head: str | None = None,
              branche: str = BRANCHE, titre: str | None = None, created: float | None = None,
              pr_payload: dict | None = None) -> dict:
    """Une exécution de `tests.yml` telle que GitHub la rend.

    Le titre (run-name) porte la PR, l'étiquette (mode + SHA complet) et la
    base ; `head` force une tête GitHub différente du titre, `titre` un ancien
    run sans étiquette, `event` un push ou un ancien dispatch.
    """
    if titre is None:
        titre = ("tests [PR #%d ci-%s-%s base %s]" % (number, mode, sha, base)
                 if event == "pull_request" else "un commit")
    row = {"id": ident, "status": statut, "conclusion": conclusion,
           "head_sha": sha if head is None else head, "head_branch": branche,
           "event": event, "display_title": titre, "name": "tests",
           "html_url": "https://github.com/%s/actions/runs/%d" % (DEPOT, ident)}
    if created is not None:
        row["created_at"] = datetime.datetime.fromtimestamp(
            created, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    if pr_payload is not None:
        row["pull_requests"] = [pr_payload]
    return row


def check_run(ident: int = 900, *, name: str = "tests", statut: str = "completed",
              conclusion: str = "success") -> dict:
    return {"id": ident, "name": name, "status": statut, "conclusion": conclusion,
            "details_url": "https://github.com/%s/runs/%d" % (DEPOT, ident)}


def marque(state: str, *, sha: str = GEL, base: str = BASE, mode: str = "lot",
           token: str = "0" * 16, pr: int = PR, label: str | None = None,
           ident: int = 1, ts: float | None = None, texte: str = "") -> dict:
    """Un événement `ci-label` du journal du lot, comme `work.events` le rend."""
    label = label or "ci-%s-%s" % (mode, sha)
    note = ("ci-label %s pr=%d label=%s sha=%s base=%s mode=%s token=%s"
            % (state, pr, label, sha, base, mode, token))
    if texte:
        note += ": " + texte
    return {"id": ident, "note": note, "created_ts": time.time() if ts is None else ts}


def porte(state: str, *, sha: str = GEL, base: str = BASE, mode: str = "lot",
          token: str = "0" * 16, ident: int = 1, ts: float | None = None) -> dict:
    """Une marque déjà parsée, comme `ci.request_events` la rend."""
    return {"id": ident, "state": state, "pr": PR,
            "label": "ci-%s-%s" % (mode, sha), "sha": sha, "base": base,
            "mode": mode, "token": token, "ts": time.time() if ts is None else ts}


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

    def __init__(self, *, head: str = GEL, runs=(), workflow: str | None = WORKFLOW,
                 pulls=None, checks=None):
        self.head = head
        self.runs = list(runs)
        self.workflow = workflow
        self.pulls = [pull()] if pulls is None else pulls
        self.checks = {"check_runs": [check_run()]} if checks is None else checks
        self.paths: list[str] = []

    def rows(self) -> list[dict]:
        """Les exécutions que la liste rend (une sous-classe peut en ajouter)."""
        return list(self.runs)

    def _pulls_now(self) -> list[dict]:
        if callable(self.pulls):
            return self.pulls()
        if len(self.pulls) > 1:
            return [self.pulls.pop(0)]
        return list(self.pulls)

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
            found = re.search(r"[?&]event=([^&]+)", path)
            if found:
                event = unquote(found.group(1))
                runs = [r for r in runs if (r.get("event") or "") == event]
            return {"workflow_runs": runs}
        if "/pulls?" in path:
            return self._pulls_now()
        if "/contents/.github/workflows/tests.yml" in path:
            if self.workflow is None:
                return {}
            return {"content": base64.b64encode(self.workflow.encode()).decode()}
        if "/check-runs?" in path:
            return self.checks
        raise AssertionError("chemin GitHub inattendu : %s" % path)

    def ecrit(self) -> list[str]:
        """Les lectures faites seulement avant une demande (le workflow)."""
        return [p for p in self.paths if "/contents/" in p]


class Gh:
    """Le `gh` des écritures : enregistre les argv, pose les étiquettes.

    `refuse` contient des fragments d'argv dont l'appel échoue ; `actor` est
    l'identité rendue par `api user` ('' la fait échouer).
    """

    def __init__(self, *, actor: str = "smichea", refuse=(), repo_labels=()):
        self.calls: list[list[str]] = []
        self.actor = actor
        self.refuse = tuple(refuse)
        self.repo_labels: set[str] = set(repo_labels)
        self.posed: list[str] = []
        self.removed: list[str] = []

    def run(self, argv: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if argv[:2] == ["api", "user"]:
            if not self.actor:
                return 1, "", "no authenticated user"
            return 0, self.actor + "\n", ""
        for frag in self.refuse:
            if frag in " ".join(argv):
                return 1, "", "refused: %s" % frag
        if argv[:3] == ["api", "-X", "POST"] and "/issues/%d/labels" % PR in argv[3]:
            names = json.loads(stdin or "{}").get("labels") or []
            self.posed += names
            self.repo_labels.update(names)
            return 0, "", ""
        if argv[:3] == ["api", "-X", "DELETE"] and "/issues/%d/labels/" % PR in argv[3]:
            self.removed.append(argv[3].rsplit("/", 1)[-1])
            return 0, "", ""
        if argv[:3] == ["api", "-X", "POST"] and argv[3] == "repos/%s/labels" % DEPOT:
            name = json.loads(stdin or "{}").get("name") or ""
            self.repo_labels.add(name)
            return 0, "", ""
        if argv[:3] == ["api", "-X", "DELETE"] and argv[3].startswith("repos/%s/labels/" % DEPOT):
            self.repo_labels.discard(argv[3].rsplit("/", 1)[-1])
            return 0, "", ""
        if argv[:2] == ["api", "repos/%s/labels/" % DEPOT]:
            # lecture d'existence : 0 si l'étiquette du dépôt existe déjà
            return (0 if argv[2].rsplit("/", 1)[-1] in self.repo_labels else 1), "", ""
        return 0, "", ""

    def annulations(self) -> list[list[str]]:
        return [c for c in self.calls if len(c) >= 3 and c[:2] == ["api", "-X"]
                and c[3].endswith("/cancel")]

    def poses(self) -> list[str]:
        return list(self.posed)



class RecordingGh(Gh):
    """Le `gh` qui retient si la pose s'appelle transaction ouverte."""

    def __init__(self, db, **kw):
        super().__init__(**kw)
        self.db = db
        self.open_poses = 0

    def run(self, argv, stdin=None):
        if argv[:3] == ["api", "-X", "POST"] and "/issues/%d/labels" % PR in argv[3] \
                and self.db.tx_open:
            self.open_poses += 1
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
    """`ci.gel_plan` : ce qui refuse, ce qui ne repose pas, ce qui part."""

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
        self.assertEqual(api.ecrit(), [], "aucun workflow lu")

    def test_une_demande_pose_l_etiquette_du_sha_gele_et_de_sa_base(self):
        self.gele()
        api = Github()
        decision = self.gel(api)
        self.assertEqual(decision["action"], "request")
        self.assertEqual((decision["sha"], decision["base"], decision["mode"]),
                         (GEL, BASE, "lot"))
        self.assertEqual((decision["pr"], decision["label"], decision["path"]),
                         (PR, LABEL, "pr-label"))
        self.assertEqual(decision["verdicts"], ["relecteur"])
        self.assertIn("/pulls?", " ".join(api.paths))
        self.assertTrue(api.ecrit(), "le déclencheur du workflow est vérifié")

    def test_un_lot_sans_pr_ouverte_est_refuse(self):
        self.gele()
        api = Github(pulls=[])
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api)
        self.assertIn("no open pull request", str(caught.exception))
        self.assertEqual(api.ecrit(), [], "aucune demande sans PR")

    def test_une_pr_sur_une_autre_tete_est_refusee(self):
        self.gele()
        api = Github(pulls=[pull(head=TETE)])
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api)
        self.assertIn("the PR moved", str(caught.exception))
        self.assertEqual(api.ecrit(), [])

    def test_une_base_illisible_est_refusee(self):
        self.gele()
        api = Github(pulls=[pull(base="pas-un-sha")])
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api)
        self.assertIn("unusable base commit", str(caught.exception))
        self.assertEqual(api.ecrit(), [])

    def test_une_source_release_refuse_le_lot_et_impose_full(self):
        self.gele()
        api = Github(pulls=[pull(head_ref="release/1.10.0")])
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api)
        self.assertIn("release/**", str(caught.exception))
        self.assertEqual(api.ecrit(), [], "le refus précède la vérification du workflow")
        decision = self.gel(Github(pulls=[pull(head_ref="release/1.10.0")]),
                            full=True, reason="release 1.10.0")
        self.assertEqual((decision["action"], decision["mode"], decision["label"]),
                         ("request", "full", LABEL_FULL))

    def test_une_cible_integration_refuse_le_lot(self):
        self.gele()
        api = Github(pulls=[pull(base_ref="integration/paquet")])
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api)
        self.assertIn("integration/**", str(caught.exception))
        decision = self.gel(Github(pulls=[pull(base_ref="integration/paquet")]),
                            full=True, reason="paquet §12")
        self.assertEqual((decision["action"], decision["mode"]), ("request", "full"))

    def test_un_full_sur_pr_ordinaire_est_une_demande_ci_full(self):
        # #1343 : le mode est porté par l'étiquette (`ci-lot` / `ci-full`) ; un
        # `--full --reason` sur une PR ordinaire est représentable et sans
        # ambiguïté, il ne peut pas verdir un run lot.
        self.jalons.append(jalon("frozen", sha=GEL))
        decision = self.gel(Github(), full=True, reason="paquet §12")
        self.assertEqual((decision["action"], decision["mode"], decision["label"]),
                         ("request", "full", LABEL_FULL))

    def test_un_vert_de_la_composition_ne_repose_pas(self):
        self.gele()
        api = Github(runs=[label_run(7, conclusion="success")])
        decision = self.gel(api)
        self.assertEqual((decision["action"], decision["run"]["id"]), ("green", 7))
        self.assertEqual(api.ecrit(), [], "un vert ne relit pas le workflow")

    def test_un_push_vert_de_la_tete_ne_remplace_pas_le_check_pr(self):
        # P1-2 : le parcours PR étiqueté seul compte ; un push vert du même
        # commit ne satisfait pas le check requis de la PR.
        self.gele()
        api = Github(runs=[label_run(7, event="push", titre="un commit")])
        self.assertEqual(self.gel(api)["action"], "request")

    def test_un_ancien_dispatch_vert_ne_remplace_pas_le_check_pr(self):
        self.gele()
        api = Github(runs=[label_run(7, event="workflow_dispatch",
                                     titre="tests lot %s lot #1343" % GEL)])
        self.assertEqual(self.gel(api)["action"], "request")

    def test_une_pr_d_avant_1343_ne_verdit_pas(self):
        # un run de PR vers main sans étiquette (d'avant #1343) ne prouve rien.
        self.gele()
        api = Github(runs=[label_run(7, titre="un commit de lot")])
        self.assertEqual(self.gel(api)["action"], "request")

    def test_un_vert_d_une_autre_base_ne_verdit_pas(self):
        # P1-3 : la base seule peut bouger sans que la tête change ; la
        # composition n'est plus celle du payload, une nouvelle demande part.
        self.gele()
        api = Github(runs=[label_run(7, base=AUTRE)])
        self.assertEqual(self.gel(api)["action"], "request")

    def test_un_vert_de_mode_lot_ne_fait_pas_un_full_deja_vert(self):
        # P1-3 : un run `lot` vert ne prouve pas la suite complète qu'un
        # `--full` demande ; la demande part.
        self.jalons.append(jalon("frozen", sha=GEL))
        api = Github(runs=[label_run(7, mode="lot")])
        decision = self.gel(api, full=True, reason="paquet §12")
        self.assertEqual((decision["action"], decision["mode"]), ("request", "full"))

    def test_un_vert_full_couvre_une_demande_lot(self):
        # la suite complète inclut les impactés et le contrat : elle couvre le
        # lot, jamais l'inverse.
        self.gele()
        api = Github(runs=[label_run(7, mode="full")])
        decision = self.gel(api)
        self.assertEqual((decision["action"], decision["run"]["id"]), ("green", 7))

    def test_un_titre_dont_la_tete_ne_correspond_pas_ne_verdit_pas(self):
        # `head_sha` GitHub doit nommer la même tête que l'étiquette du titre.
        self.gele()
        api = Github(runs=[label_run(7, head=TETE)])
        self.assertEqual(self.gel(api)["action"], "request")

    def test_l_api_peut_nommer_le_merge_ref_dans_le_payload(self):
        # repli : si l'API liste le merge-ref, le payload de la PR nomme la
        # tête gelée et le run reste applicable.
        self.gele()
        api = Github(runs=[label_run(7, head=TETE,
                                     pr_payload={"head": {"sha": GEL}})])
        decision = self.gel(api)
        self.assertEqual((decision["action"], decision["run"]["id"]), ("green", 7))

    def test_une_execution_en_cours_ne_repose_pas(self):
        self.gele()
        api = Github(runs=[label_run(8, statut="in_progress", conclusion=None)])
        self.assertEqual(self.gel(api)["action"], "running")

    def test_un_rouge_demande_force(self):
        self.gele()
        rouge = [label_run(9, conclusion="failure")]
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github(runs=rouge))
        self.assertIn("--force", str(caught.exception))
        self.assertEqual(self.gel(Github(runs=rouge), force=True)["action"], "request")

    def test_force_ne_rejoue_pas_un_vert(self):
        # P2 R1 : `--force` ne lève pas l'idempotence d'un vert applicable.
        self.gele()
        api = Github(runs=[label_run(7, conclusion="success")])
        decision = self.gel(api, force=True)
        self.assertEqual((decision["action"], decision["run"]["id"]), ("green", 7))

    def test_force_ne_rejoue_pas_un_run_en_vol(self):
        # P2 R1 : ni celle d'un run en cours.
        self.gele()
        api = Github(runs=[label_run(8, statut="queued", conclusion=None)])
        self.assertEqual(self.gel(api, force=True)["action"], "running")

    def test_un_check_requis_rouge_demande_force(self):
        # une étiquette étrangère peut laisser le dernier check requis rouge
        # alors qu'un vert applicable existe : la reprise est explicite.
        self.gele()
        api = Github(runs=[label_run(7)],
                     checks={"check_runs": [check_run(901, conclusion="failure")]})
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api)
        self.assertIn("--force", str(caught.exception))
        self.assertIn("failure", str(caught.exception))
        self.assertEqual(self.gel(Github(runs=[label_run(7)],
                                         checks={"check_runs": [
                                             check_run(901, conclusion="failure")]}),
                                  force=True)["action"], "request")

    def test_un_check_requis_en_vol_attend(self):
        self.gele()
        api = Github(runs=[label_run(7)],
                     checks={"check_runs": [check_run(901, statut="in_progress",
                                                     conclusion=None)]})
        decision = self.gel(api)
        self.assertEqual(decision["action"], "running")
        self.assertIn("check", decision)

    def test_une_reservation_fraiche_ne_repose_pas(self):
        # P2 R1 : une étiquette posée mais pas encore visible comme exécution
        # tient la clé ; GitHub ne la montre qu'avec un délai.
        self.gele()
        self.events.append(marque("applied", token="1" * 16))
        api = Github()
        decision = self.gel(api)
        self.assertEqual(decision["action"], "running")
        self.assertIn("reserved", decision["why"])
        self.assertEqual(api.ecrit(), [], "une réservation ne relit pas le workflow")

    def test_une_reservation_perimee_laisse_reposer(self):
        self.gele()
        self.events.append(marque("applied", token="1" * 16, ts=0.0))
        self.assertEqual(self.gel(Github())["action"], "request")

    def test_une_marque_released_ne_tient_pas_la_cle(self):
        self.gele()
        self.events.append(marque("released", token="1" * 16, texte="pose refusée"))
        self.assertEqual(self.gel(Github())["action"], "request")

    def test_une_reservation_d_une_autre_base_ne_tient_pas(self):
        self.gele()
        self.events.append(marque("applied", base=AUTRE, token="1" * 16))
        self.assertEqual(self.gel(Github())["action"], "request")

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
        # tête, pas le gel périmé, et la demande est marquée.
        self.gele()
        api = Github(head=TETE, pulls=[pull(head=TETE)])
        decision = self.gel(api, exception="incident", reason="incident prod")
        self.assertEqual((decision["action"], decision["sha"]), ("request", TETE))
        self.assertEqual(decision["base"], BASE)

    def test_un_workflow_sans_declencheur_est_refuse(self):
        self.gele()
        api = Github(workflow="name: tests\n")
        with self.assertRaises(ci.GelError) as caught:
            self.gel(api)
        self.assertIn("does not carry the freeze request trigger", str(caught.exception))

    def test_une_exception_sans_gel_ni_avis_mais_marquee(self):
        api = Github(head=TETE, pulls=[pull(head=TETE)])
        decision = self.gel(api, exception="incident", reason="CI cassée sur main")
        self.assertEqual(decision["action"], "request")
        self.assertEqual((decision["sha"], decision["exception"]), (TETE, "incident"))
        self.assertIn("CI cassée sur main", decision["reason"])

    def test_une_exception_sans_reason_est_refusee(self):
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github(), exception="unblock")
        self.assertIn("requires --reason", str(caught.exception))
        with self.assertRaises(ci.GelError):
            self.gel(Github(), exception="autre", reason="x")

    def test_full_sans_avis_en_mode_full(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        decision = self.gel(Github(), full=True, reason="paquet §12")
        self.assertEqual((decision["action"], decision["mode"]), ("request", "full"))

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
        self.assertEqual(self.gel(Github(), avis=2)["action"], "request")

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
        self.assertEqual(self.gel(Github())["action"], "request")

    def test_un_blocked_d_un_autre_relecteur_bloque_la_tete(self):
        # P1 R1 : la tête refusée par un relecteur ne part pas, même si un
        # autre a dit `ok` — une correction demande une nouvelle tête.
        self.jalons += [jalon("frozen", sha=GEL),
                        jalon("verdict", sha=GEL, actor="r1", verdict="ok"),
                        jalon("verdict", sha=GEL, actor="r2", verdict="blocked")]
        with self.assertRaises(ci.GelError) as caught:
            self.gel(Github(), avis=1)
        self.assertIn("r2", str(caught.exception))

    def test_un_lot_leger_demande_sans_avis(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        self.assertEqual(self.gel(Github(), avis=0)["action"], "request")


class ReservationTest(unittest.TestCase):
    """`ci.reservation_holder` : la clé d'une demande pas encore visible (P2 R1).

    R1 delta : la réservation suit la couverture des modes (un `full` couvre
    le `lot`) et une marque antérieure à l'exécution visible est matérialisée
    par elle — le `--force` d'un run rouge court peut repartir.
    """

    def test_la_reservation_full_couvre_la_demande_lot(self):
        now = time.time()
        full = [porte("applied", mode="full", token="6" * 16)]
        self.assertIsNotNone(ci.reservation_holder(full, GEL, BASE, "lot", "", now=now))
        self.assertIsNotNone(ci.reservation_holder(full, GEL, BASE, "full", "", now=now))
        lot = [porte("applied", mode="lot", token="6" * 16)]
        self.assertIsNone(ci.reservation_holder(lot, GEL, BASE, "full", "", now=now))
        self.assertIsNotNone(ci.reservation_holder(lot, GEL, BASE, "lot", "", now=now))

    def test_une_marque_anterieure_au_run_visible_ne_tient_pas(self):
        now = time.time()
        avant = [porte("applied", token="7" * 16, ts=now - 300)]
        self.assertIsNone(ci.reservation_holder(avant, GEL, BASE, "lot", "",
                                                now=now, after=now - 60))
        apres = [porte("applied", token="7" * 16, ts=now)]
        self.assertIsNotNone(ci.reservation_holder(apres, GEL, BASE, "lot", "",
                                                   now=now, after=now - 60))


class CancelPlanTest(GelTestCase):
    """`ci.cancel_plan` : les exécutions non conclues, la composition gardée."""

    def test_seuls_les_runs_ouverts_hors_sha_gele_sont_annules(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        api = Github(runs=[label_run(1, statut="in_progress", conclusion=None, sha=GEL),
                           label_run(2, statut="queued", conclusion=None, sha=TETE),
                           label_run(3, conclusion="success", sha=TETE)])
        report = self.annule(api)
        self.assertEqual([r["id"] for r in report["runs"]], [2])
        self.assertIn(GEL[:12], report["why"])

    def test_la_meme_tete_sur_une_autre_base_est_perimee(self):
        # #1343 : la composition courante (PR, tête, base) est gardée ; la même
        # tête sur une autre base a une composition périmée, elle part.
        self.jalons.append(jalon("frozen", sha=GEL))
        api = Github(runs=[label_run(1, statut="in_progress", conclusion=None, base=BASE),
                           label_run(2, statut="queued", conclusion=None, base=AUTRE)])
        report = self.annule(api, number=PR, base=BASE)
        self.assertEqual([r["id"] for r in report["runs"]], [2])

    def test_le_sha_gele_s_annule_avec_all(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        api = Github(runs=[label_run(1, statut="in_progress", conclusion=None, sha=GEL),
                           label_run(2, statut="queued", conclusion=None, sha=TETE)])
        report = self.annule(api, all_runs=True)
        self.assertEqual(sorted(r["id"] for r in report["runs"]), [1, 2])
        self.assertTrue(report["all"])

    def test_un_lot_sans_gel_annule_tous_ses_runs_ouverts(self):
        api = Github(runs=[label_run(1, statut="queued", conclusion=None, sha=GEL),
                           label_run(2, conclusion="failure", sha=TETE)])
        self.assertEqual([r["id"] for r in self.annule(api)["runs"]], [1])

    def test_un_lot_sans_branche_refuse(self):
        self.item = lot(branch="")
        with self.assertRaises(ci.GelError) as caught:
            self.annule(Github())
        self.assertIn("nothing to cancel", str(caught.exception))


class RequestTest(GelTestCase):
    """`ci.cmd_ci_run` : annule les périmés, pose l'étiquette une fois, note."""

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
        if self.on_note and text.startswith("ci-label reserve"):
            self.on_note(text)
        return True

    def run_cmd(self, api, gh, *, args=None):
        """`cmd_ci_run` avec le `gh`, la base et la note remplacés."""
        with mock.patch("ameesh.db.open_db", lambda cfg: self.db), \
                mock.patch.object(plan_github, "Gh", lambda *a, **k: gh), \
                mock.patch.object(plan_github, "_json",
                                  lambda g, argv, *a, **k: api(argv[-1])), \
                contextlib.redirect_stdout(io.StringIO()):
            code = ci.cmd_ci_run(None, args or self.args)
        return code, self.notes

    def marques(self, state: str) -> list[str]:
        return [n for n in self.notes if n.startswith("ci-label %s" % state)]

    def test_la_demande_annule_les_perimes_puis_pose_l_etiquette(self):
        self.gele()
        api = Github(runs=[label_run(4, statut="queued", conclusion=None, sha=TETE)])
        gh = Gh()
        code, notes = self.run_cmd(api, gh)
        self.assertEqual(code, 0)
        self.assertEqual([c[-1] for c in gh.annulations()],
                         ["repos/%s/actions/runs/4/cancel" % DEPOT])
        self.assertEqual(gh.poses(), [LABEL])
        applied = self.marques("applied")
        self.assertEqual(len(applied), 1)
        self.assertIn("PR #%d" % PR, applied[0])
        self.assertIn(LABEL, applied[0])
        self.assertIn("base %s" % BASE, applied[0])
        self.assertEqual(len(self.marques("reserve")), 1, notes)

    def test_dry_run_ne_pose_rien(self):
        self.gele()
        api = Github(runs=[label_run(4, statut="queued", conclusion=None, sha=TETE)])
        gh = Gh()
        code, notes = self.run_cmd(api, gh, args=argparse.Namespace(
            **{**vars(self.args), "dry_run": True}))
        self.assertEqual((code, gh.calls, notes), (0, [], []))

    def test_un_jeton_actions_est_refuse_avant_toute_pose(self):
        # #1343 : l'événement d'un GITHUB_TOKEN d'Actions ne déclenche rien.
        self.gele()
        gh = Gh(actor="github-actions[bot]")
        err = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(err):
            code, notes = self.run_cmd(Github(), gh)
        self.assertEqual(code, 1)
        self.assertEqual(gh.poses(), [])
        self.assertIn("github-actions[bot]", notes[-1])
        self.assertEqual(len(self.marques("released")), 1)

    def test_une_pose_refusee_est_dite_et_note(self):
        self.gele()
        gh = Gh(refuse=("/issues/%d/labels" % PR,))
        code, notes = self.run_cmd(Github(), gh)
        self.assertEqual(code, 1)
        released = self.marques("released")
        self.assertEqual(len(released), 1)
        self.assertIn("cannot pose the label", released[0])
        self.assertEqual(self.marques("applied"), [])

    def test_une_etiquette_deja_posee_est_retiree_puis_reposee(self):
        self.gele()
        api = Github(pulls=[pull(labels=[LABEL])])
        gh = Gh(repo_labels={LABEL})
        code, _notes = self.run_cmd(api, gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.removed, [LABEL])
        self.assertEqual(gh.poses(), [LABEL])
        self.assertIn(LABEL, gh.repo_labels, "l'étiquette du dépôt n'est pas supprimée")

    def test_les_etiquettes_perimees_sont_retirees_et_supprimees(self):
        self.gele()
        vieux = "ci-lot-%s" % AUTRE
        api = Github(pulls=[pull(labels=[vieux])])
        gh = Gh(repo_labels={vieux})
        code, _notes = self.run_cmd(api, gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.removed, [vieux])
        self.assertEqual(gh.poses(), [LABEL])
        self.assertNotIn(vieux, gh.repo_labels, "l'étiquette périmée quitte le dépôt")

    def test_une_etiquette_etrangere_n_est_jamais_touchee(self):
        self.gele()
        api = Github(pulls=[pull(labels=["triage", "ci-ancien"])])
        gh = Gh()
        code, _notes = self.run_cmd(api, gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.removed, [])
        self.assertEqual(gh.poses(), [LABEL])

    def test_l_etiquette_du_depot_est_creee_si_elle_manque(self):
        self.gele()
        gh = Gh()
        self.assertEqual(gh.repo_labels, set())
        self.run_cmd(Github(), gh)
        self.assertEqual(gh.repo_labels, {LABEL})

    def test_une_demande_deja_reservee_recule(self):
        # P2 R1 : la marque fraîche d'un autre fait reculer le second dès la
        # décision — aucune pose, aucun `gh`, aucune note.
        self.gele()
        self.events.append(marque("applied", token="1" * 16))
        gh = Gh()
        code, notes = self.run_cmd(Github(), gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.calls, [], "aucun geste gh, pas même une annulation")
        self.assertEqual(notes, [])

    def test_un_run_qui_apparait_sous_le_verrou_recule(self):
        # P2 R1 : la course inter-hôtes — la décision a vu la place vide, mais
        # l'exécution d'un autre devient visible pendant l'attente du verrou ;
        # la relecture sous le verrou recule.
        self.gele()
        api = Github()
        self.on_note = lambda text: api.runs.append(
            label_run(11, statut="queued", conclusion=None))
        gh = Gh()
        code, notes = self.run_cmd(api, gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.poses(), [])
        self.assertEqual(self.marques("applied"), [])
        self.assertEqual(len(self.marques("reserve")), 1)

    def test_une_marque_concurrente_sous_le_verrou_recule(self):
        # P2 R1 : un autre hôte a commité sa marque entre la décision et le
        # verrou ; le second la lit et recule, en refermant la sienne.
        self.gele()
        self.on_note = lambda text: self.events.append(
            marque("applied", token="2" * 16, ident=99))
        gh = Gh()
        code, notes = self.run_cmd(Github(), gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.poses(), [])
        self.assertEqual(self.marques("applied"), [])
        self.assertEqual(len(self.marques("released")), 1)

    def test_la_pr_bougee_sous_le_verrou_recule(self):
        self.gele()
        api = Github(pulls=[pull(), pull(head=TETE)])
        gh = Gh()
        code, notes = self.run_cmd(api, gh)
        self.assertEqual(code, 1)
        self.assertEqual(gh.poses(), [])
        self.assertIn("head moved", notes[-1])

    def test_la_base_bougee_sous_le_verrou_recule(self):
        self.gele()
        api = Github(pulls=[pull(), pull(base=AUTRE)])
        gh = Gh()
        code, notes = self.run_cmd(api, gh)
        self.assertEqual(code, 1)
        self.assertEqual(gh.poses(), [])
        self.assertIn("base moved", notes[-1])

    def test_la_pose_s_appelle_dans_la_transaction_du_lot(self):
        # P2 R1 : la pose tient le verrou de la ligne `work_items` (pris par la
        # note `reserve`) ; deux `ci run` concurrents, même d'hôtes différents,
        # se sérialisent.
        self.gele()
        gh = RecordingGh(self.db)
        code, _notes = self.run_cmd(Github(), gh)
        self.assertEqual(code, 0)
        self.assertEqual(self.db.transactions, 1)
        self.assertEqual(gh.open_poses, 1, "la pose s'appelle transaction ouverte")
        self.assertFalse(self.db.tx_open, "la transaction est refermée")

    def test_une_exception_marque_le_motif_dans_la_note(self):
        self.jalons.append(jalon("frozen", sha=GEL))
        gh = Gh()
        code, notes = self.run_cmd(Github(), gh, args=argparse.Namespace(
            **{**vars(self.args), "exception": "unblock", "reason": "main rouge"}))
        self.assertEqual(code, 0)
        self.assertIn(LABEL, gh.poses())
        self.assertTrue(any("exception unblock: main rouge" in n for n in notes))

    #: R1 delta Codex (3ce572f7b) : la relecture et la décision se refont sous
    #: le verrou commun, juste avant la pose.

    def test_un_verdict_bloque_sous_le_verrou_recule(self):
        # P1 delta : le plan a lu les verdicts avant le verrou ; le `blocked`
        # commité par le relecteur pendant l'attente doit être relu sous le
        # verrou — sinon la commande pose l'étiquette et démarre la CI.
        self.gele()
        self.on_note = lambda text: self.jalons.append(
            jalon("verdict", sha=GEL, actor="relecteur", verdict="blocked"))
        gh = Gh()
        code, notes = self.run_cmd(Github(), gh)
        self.assertEqual(code, 1)
        self.assertEqual(gh.poses(), [])
        self.assertEqual(self.marques("applied"), [])
        self.assertIn("blocked", notes[-1])
        self.assertEqual(len(self.marques("reserve")), 1)

    def test_un_gel_deplace_sous_le_verrou_recule(self):
        # P1 delta : un nouveau gel enregistré entre le plan et le verrou
        # change la tête autorisée ; la demande préparée sur l'ancienne recule.
        self.gele()
        self.on_note = lambda text: self.jalons.append(jalon("frozen", sha=TETE))
        gh = Gh()
        code, notes = self.run_cmd(Github(), gh)
        self.assertEqual(code, 1)
        self.assertEqual(gh.poses(), [])
        self.assertIn("freeze", notes[-1])
        self.assertIn(TETE[:12], notes[-1])

    def test_une_exception_ne_relit_pas_les_avis(self):
        # P1 delta, exceptions conservées : `--exception … --reason` reste un
        # geste explicite et tracé ; un `blocked` arrivé pendant l'attente du
        # verrou ne l'annule pas.
        self.jalons.append(jalon("frozen", sha=GEL))
        self.on_note = lambda text: self.jalons.append(
            jalon("verdict", sha=GEL, actor="relecteur", verdict="blocked"))
        gh = Gh()
        code, _notes = self.run_cmd(Github(), gh, args=argparse.Namespace(
            **{**vars(self.args), "exception": "unblock", "reason": "main rouge"}))
        self.assertEqual(code, 0)
        self.assertIn(LABEL, gh.poses())

    def test_force_repare_le_check_rouge_sous_le_verrou(self):
        # P2 delta : le plan voit un vert applicable ET le dernier check
        # `tests` rouge, et décide `request` avec --force ; la phase critique
        # doit réappliquer la même décision au lieu de rendre `green` sans rien
        # poser — la reprise du check requis ne partait pas.
        self.gele()
        api = Github(runs=[label_run(5, conclusion="success")],
                     checks={"check_runs": [check_run(901, conclusion="failure")]})
        gh = Gh()
        code, notes = self.run_cmd(api, gh, args=argparse.Namespace(
            **{**vars(self.args), "force": True}))
        self.assertEqual(code, 0)
        self.assertEqual(gh.poses(), [LABEL])
        self.assertEqual(len(self.marques("applied")), 1, notes)

    def test_un_run_rouge_visible_ne_tient_plus_la_reservation(self):
        # P2 delta : après un run rouge court, la marque `applied` de ce run
        # tient encore la clé quinze minutes ; `--force` doit repartir — la
        # marque antérieure au run visible est matérialisée par lui.
        self.gele()
        t = time.time()
        self.events.append(marque("applied", token="4" * 16, ts=t - 300))
        api = Github(runs=[label_run(12, conclusion="failure", created=t - 60)])
        gh = Gh()
        code, notes = self.run_cmd(api, gh, args=argparse.Namespace(
            **{**vars(self.args), "force": True}))
        self.assertEqual(code, 0)
        self.assertEqual(gh.poses(), [LABEL])
        self.assertEqual(len(self.marques("applied")), 1, notes)

    def test_une_marque_posterieure_au_run_visible_tient_la_cle(self):
        # P2 delta, garde : une marque d'un autre hôte postérieure au run
        # visible n'est pas matérialisée par lui ; elle tient toujours la clé.
        self.gele()
        t = time.time()
        self.events.append(marque("applied", token="5" * 16, ts=t))
        api = Github(runs=[label_run(12, conclusion="failure", created=t - 60)])
        gh = Gh()
        code, notes = self.run_cmd(api, gh, args=argparse.Namespace(
            **{**vars(self.args), "force": True}))
        self.assertEqual(code, 0)
        self.assertEqual(gh.poses(), [])
        self.assertTrue(any("already holds" in n for n in notes), notes)

    def test_une_reservation_full_couvre_la_demande_lot(self):
        # P2 delta : avant que le run `full` soit visible dans l'API, sa marque
        # couvre une demande `lot` de la même composition : une seule pose.
        self.gele()
        gh = Gh()
        full = argparse.Namespace(**{**vars(self.args), "full": True,
                                     "reason": "paquet"})
        code, notes = self.run_cmd(Github(), gh, args=full)
        self.assertEqual(code, 0)
        self.assertEqual(gh.poses(), [LABEL_FULL])
        avant = len(notes)
        code, notes = self.run_cmd(Github(), gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.poses(), [LABEL_FULL], "le full couvre le lot")
        self.assertEqual(len(notes), avant, "la demande lot recule avant toute note")

    def test_une_reservation_lot_ne_couvre_pas_la_demande_full(self):
        # P2 delta, garde : un `lot` réservé ne dispense pas du `full` ; la
        # demande pleine part (le run `lot` ne prouve pas la suite complète).
        self.gele()
        gh = Gh()
        code, _notes = self.run_cmd(Github(), gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.poses(), [LABEL])
        full = argparse.Namespace(**{**vars(self.args), "full": True,
                                     "reason": "paquet"})
        code, _notes = self.run_cmd(Github(), gh, args=full)
        self.assertEqual(code, 0)
        self.assertEqual(gh.poses(), [LABEL, LABEL_FULL])

    def test_le_chemin_medie_reste_sans_verrou_de_ligne(self):
        # Observation R1 : sans transaction (connexion d'exécuteur médiée,
        # L109), la marque reste une réservation de meilleur effort ; la
        # sérialisation par verrou de ligne est bornée aux connexions directes.
        self.gele()
        gh = Gh()
        with mock.patch.object(ci.storage, "is_remote", lambda db: True):
            code, _notes = self.run_cmd(Github(), gh)
        self.assertEqual(code, 0)
        self.assertEqual(self.db.transactions, 0)
        self.assertEqual(gh.poses(), [LABEL])


class ParsingTest(unittest.TestCase):
    """`ci.pr_run`, `ci.label_name`, le parseur : sans base ni réseau."""

    def test_label_name_longueurs_exactes(self):
        self.assertEqual(ci.label_name("lot", GEL), "ci-lot-%s" % GEL)
        self.assertEqual(ci.label_name("full", GEL), "ci-full-%s" % GEL)
        self.assertEqual(len(ci.label_name("lot", GEL)), 47)
        self.assertEqual(len(ci.label_name("full", GEL)), 48)
        with self.assertRaises(ci.GelError):
            ci.label_name("lot", GEL[:12])
        with self.assertRaises(ci.GelError):
            ci.label_name("autre", GEL)

    def test_pr_run_ignore_les_titres_sans_etiquette(self):
        for row in (label_run(1, titre="un commit"),
                    label_run(2, event="push", titre="un commit"),
                    label_run(3, event="workflow_dispatch",
                              titre="tests lot %s lot #1343" % GEL),
                    label_run(4, titre="tests [PR #%d ci-lot-%s base %s]"
                                       % (PR, GEL[:12], BASE)),
                    label_run(5, head=TETE)):
            with self.subTest(row["id"]):
                self.assertIsNone(ci.pr_run(row))

    def test_pr_run_lit_mode_sha_base_et_pr(self):
        info = ci.pr_run(label_run(6, mode="full", base=AUTRE))
        self.assertEqual((info["number"], info["mode"], info["sha"], info["base"]),
                         (PR, "full", GEL, AUTRE))

    def test_release_refs(self):
        self.assertTrue(ci.release_refs("release/1.10.0", "main"))
        self.assertTrue(ci.release_refs("lot/x", "integration/paquet"))
        self.assertFalse(ci.release_refs("lot/x", "main"))

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
