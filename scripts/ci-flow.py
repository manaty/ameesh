#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Lecture du flux des résultats de tests (L161b).

    scripts/ci-flow.py summary F.jsonl [F.jsonl…]   >> "$GITHUB_STEP_SUMMARY"
    scripts/ci-flow.py publish F.jsonl --name "tests (flux) psql 1/6" &   (L161c)
    scripts/ci-flow.py close F.jsonl --name "tests (flux) psql 1/6"      (L161c)
    scripts/ci-flow.py sweep [--sha TÊTE]                                (#856)

Le flux est écrit par `scripts/test-parts.py run --flow F.jsonl` : une
ligne JSON par événement, schéma `ameesh-test-event/1` (étude
docs/design/etudes/l161-tests-par-impact.md, section 3) :

* `debut` : une par part ; `pilote`, `modules` dans l'ordre d'exécution,
  `classes` (P0 module de test changé, P1 touché par un fichier changé, P3
  le reste), `estimee_s` ;
* `test` : une par test terminé ; `module`, `test` (nom pointé), `classe`,
  `raison` (le fichier changé qui l'a fait passer d'abord), `statut`
  (`succes`, `echec`, `erreur`, `saute`, `echec_attendu`,
  `succes_inattendu`), `duree_s`, et pour un échec `extrait` (40 lignes au
  plus) et `rejouer` (la commande qui le relance seul) ;
* `fin` : une par part ; `statut` (`succes`, `echec`, `interrompu`),
  `comptes` par statut, `duree_s` ;
* `impacted` (L161f, parts run from a plan only): once the P0 and P1
  modules of the part are done; `statut` (`vert`, `rouge`), `modules`,
  `rouges` (impacted modules that failed). When every part says `vert`, the
  impacted tests are green: the provisional green
  (`scripts/test-plan.py leaks`).

Chaque ligne porte aussi `v`, `schema`, `type`, `ts` (UTC) et le contexte
de la CI : `depot`, `tete` (commit poussé), `fusion` (commit testé),
`execution` (numéro d'exécution GitHub), `part`.

`summary` en fait le résumé Markdown du job. Une ligne illisible (fichier
coupé par l'arrêt du job) est comptée et ignorée ; un fichier absent donne
un résumé qui le dit, jamais une erreur : l'étape tourne `if: always()`.

L161c — `publish` suit le fichier pendant la part (en arrière-plan) et tient
le check run « tests (flux) <part> » de la tête poussée : chaque échec y
entre dès qu'il est écrit, en annotation (lisible dans la PR) et en ligne JSON
dans la sortie du check run, entre `MARKER` et la fin du bloc. C'est ce que
relève `ameesh notify` (`ameesh.ci`) pour l'envoyer à l'agent du lot. Il
s'arrête à la ligne `fin`. `close` (étape `if: always()`) le termine dans
tous les cas, et le crée s'il n'existe pas encore. Sans jeton, ou si GitHub
refuse (PR d'une fourche : jeton en lecture seule), rien n'est publié et
l'étape reste verte : la publication n'est jamais une cause d'échec.

#856 — un runner arrêté (saturation #747) tue le publisher *et* l'étape
`close` : le check restait `in_progress` pour toujours, la PR ne pouvait plus
paraître verte. `sweep` est la réconciliation, indépendante du publisher : il
relit chaque check « tests (flux) » encore en cours et l'état terminal réel du
job et de l'exécution qui l'ont publié, puis conclut `cancelled` — jamais
`success`, faute de ligne `fin`. L'exécution se lit dans le `details_url`
quand il porte `/actions/runs/<id>` ; sinon dans le `check_suite` du check
(GitHub laisse l'URL par défaut `…/runs/<id_du_check>` sur les checks publiés
par un jeton d'exécution). Un check dont le job tourne encore, une autre
tentative ou une autre tête ne sont pas touchés. `publish` qui atteint son
arrêt de sûreté sans ligne `fin` laisse le check ouvert (le publisher vit
encore) : `close` ou `sweep` le conclura.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

SCHEMA = "ameesh-test-event/1"
FAILURES = ("echec", "erreur")
#: tests listés au plus dans le résumé, par catégorie
LIMIT = 20


def read(chemin: str | pathlib.Path) -> tuple[list[dict], int]:
    """Les événements d'un fichier de flux et le nombre de lignes illisibles."""
    events, illisibles = [], 0
    for ligne in pathlib.Path(chemin).read_text(encoding="utf-8").splitlines():
        if not ligne.strip():
            continue
        try:
            ev = json.loads(ligne)
        except json.JSONDecodeError:
            illisibles += 1
            continue
        if isinstance(ev, dict) and ev.get("v") == 1:
            events.append(ev)
        else:
            illisibles += 1
    return events, illisibles


def _instant(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration(s: float) -> str:
    return f"{s:.1f} s" if s < 60 else f"{int(s // 60)} min {int(s % 60):02d} s"


def summary(chemins: list[str]) -> str:
    """Résumé Markdown d'un ou plusieurs fichiers de flux."""
    check_output: list[str] = []
    for chemin in chemins:
        if not pathlib.Path(chemin).is_file():
            check_output.append(f"### Flux des tests\n\nAucun flux : `{chemin}` absent "
                          "(la part n'a pas démarré).\n")
            continue
        events, illisibles = read(chemin)
        debut = next((e for e in events if e.get("type") == "debut"), {})
        fin = next((e for e in events if e.get("type") == "fin"), None)
        tests = [e for e in events if e.get("type") == "test"]
        comptes = collections.Counter(e.get("statut", "?") for e in tests)
        part = debut.get("part") or (tests[0].get("part") if tests else "") or chemin
        if fin is None:
            etat = "inachevée (pas de ligne de fin)"
        else:
            etat = {"succes": "verte", "echec": "rouge"}.get(fin.get("statut"), fin.get("statut"))
            etat += f", {_duration(fin.get('duree_s', 0))}"
        check_output.append(f"### Tests {part} : {etat}\n")
        check_output.append(", ".join(f"{n} {s}" for s, n in sorted(comptes.items())) or "aucun test")
        if illisibles:
            check_output[-1] += f" ; {illisibles} ligne(s) illisible(s)"
        check_output.append("")
        classes = debut.get("classes", {})
        touches = [m for m in debut.get("modules", []) if classes.get(m, "P3") != "P3"]
        impacted = next((e for e in events if e.get("type") == "impacted"), None)
        if impacted is not None:
            when = ""
            t0, t1 = _instant(debut.get("ts")), _instant(impacted.get("ts"))
            if t0 and t1:
                when = f" after {_duration((t1 - t0).total_seconds())}"
            check_output.append(f"Impacted modules (P0, P1): "
                          f"{'green' if impacted.get('statut') == 'vert' else 'red'}{when}\n")
        if touches:
            check_output.append("Passés d'abord (touchés par la PR) : "
                          + ", ".join(f"`{m}` ({classes[m]})" for m in touches) + "\n")
        echecs = [e for e in tests if e.get("statut") in FAILURES]
        if echecs:
            check_output.append("| statut | test | durée | rejouer |\n|---|---|---|---|")
            for e in echecs[:LIMIT]:
                rejouer = f"`{e['rejouer']}`" if e.get("rejouer") else ""
                check_output.append(f"| {e['statut']} | `{e.get('test', '?')}` | "
                              f"{e.get('duree_s', 0):.1f} s | {rejouer} |")
            if len(echecs) > LIMIT:
                check_output.append(f"\n… et {len(echecs) - LIMIT} autre(s).")
            check_output.append("")
            for e in echecs[:LIMIT]:
                if e.get("extrait"):
                    check_output.append(f"<details><summary><code>{e.get('test', '?')}</code></summary>"
                                  f"\n\n```\n{e['extrait']}\n```\n</details>\n")
        lents = sorted(tests, key=lambda e: -e.get("duree_s", 0))[:5]
        if lents:
            check_output.append("Tests les plus lents : " + ", ".join(
                f"`{e.get('test', '?').removeprefix('tests.')}` {e.get('duree_s', 0):.1f} s"
                for e in lents) + "\n")
    return "\n".join(check_output) + "\n"


# --------------------------------------------------------------------------
# L161c : check run « tests (flux) » tenu pendant la part
# --------------------------------------------------------------------------

#: début du bloc lu par `ameesh notify` (ameesh.ci.events_of)
MARKER = "<!-- ameesh-test-event/1 -->"
#: taille maximale de `output.text` d'un check run (GitHub : 65 535)
MAX_TEXT = 65000
#: échecs dont l'extrait est gardé entier, puis lignes gardées pour les suivants
FULL_EXCERPTS = 5
FOLLOWING_LINES = 10
#: annotations au plus par requête (limite de GitHub)
MAX_ANNOTATIONS = 50
CONCLUSIONS = {"succes": "success", "echec": "failure", "interrompu": "cancelled"}
#: préfixe des checks tenus par `publish` (#856 : le job porte le nom sans lui)
FLUX_PREFIX = "tests (flux) "
#: conclusion d'un check orphelin : le publisher est mort sans lire de ligne
#: `fin`, donc jamais `success` (#856)
ORPHAN_CONCLUSION = "cancelled"


def _failure_line(ev: dict, lignes: int | None) -> str:
    ev = dict(ev)
    if lignes == 0:
        ev.pop("extrait", None)
    elif lignes is not None and ev.get("extrait"):
        coupe = ev["extrait"].splitlines()
        if len(coupe) > lignes:
            ev["extrait"] = "\n".join(coupe[:lignes] + ["… (%d ligne(s) de plus)"
                                                        % (len(coupe) - lignes)])
    return json.dumps(ev, ensure_ascii=False)


def check_output(events: list[dict]) -> dict:
    """`output` du check run : titre, résumé, et le bloc des échecs."""
    debut = next((e for e in events if e.get("type") == "debut"), {})
    fin = next((e for e in events if e.get("type") == "fin"), None)
    tests = [e for e in events if e.get("type") == "test"]
    echecs = [e for e in tests if e.get("statut") in FAILURES]
    attendus = sum(1 for _ in debut.get("modules", ())) if debut else 0
    etat = "en cours" if fin is None else {"succes": "verte", "echec": "rouge"}.get(
        fin.get("statut"), fin.get("statut") or "?")
    titre = "%d échec(s), %d test(s) passé(s), %s" % (len(echecs), len(tests), etat)
    resume_ = ["Part %s : %s." % (debut.get("part") or "?", etat)]
    if attendus:
        resume_.append("%d module(s) dans cette part." % attendus)
    if echecs:
        resume_.append("")
        resume_ += ["- `%s` (%s)" % (e.get("test"), e.get("statut")) for e in echecs[:LIMIT]]
        if len(echecs) > LIMIT:
            resume_.append("- … et %d autre(s)" % (len(echecs) - LIMIT))
    lignes, taille, omis = [MARKER, "```jsonl"], len(MARKER) + 20, 0
    for i, e in enumerate(echecs):
        for garde in ((None if i < FULL_EXCERPTS else FOLLOWING_LINES), 0):
            ligne = _failure_line(e, garde)
            if taille + len(ligne) + 1 < MAX_TEXT - 200:
                lignes.append(ligne)
                taille += len(ligne) + 1
                break
        else:
            omis += 1
    lignes.append("```")
    if omis:
        lignes.append("%d échec(s) de plus, hors de la limite de taille de GitHub." % omis)
    return {"title": titre[:255], "summary": "\n".join(resume_)[:60000],
            "text": "\n".join(lignes)}


def annotations(echecs: list[dict]) -> list[dict]:
    """Une annotation par échec, sur le fichier du module de test."""
    out = []
    for e in echecs:
        module = e.get("module") or ""
        message = (e.get("extrait") or e.get("statut") or "échec")[-60000:]
        if e.get("rejouer"):
            message += "\n\nrejouer : %s" % e["rejouer"]
        out.append({"path": "tests/%s.py" % module if module else "tests",
                    "start_line": 1, "end_line": 1, "annotation_level": "failure",
                    "title": str(e.get("test") or "?")[:255], "message": message})
    return out


class GitHub:
    """Les deux appels utiles de l'API des check runs, avec le jeton du job."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.jeton = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or ""
        self.depot = env.get("GITHUB_REPOSITORY", "")
        self.api = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.tete = env.get("AMEESH_CI_TETE") or env.get("GITHUB_SHA", "")
        run = env.get("GITHUB_RUN_ID", "")
        self.lien = ("%s/%s/actions/runs/%s" % (env.get("GITHUB_SERVER_URL", "https://github.com"),
                                                self.depot, run) if run else None)

    def ready(self) -> bool:
        return bool(self.jeton and self.depot and self.tete)

    def _request(self, methode: str, chemin: str, corps: dict | None = None) -> dict:
        req = urllib.request.Request(
            "%s/repos/%s/%s" % (self.api, self.depot, chemin), method=methode,
            data=None if corps is None else json.dumps(corps).encode("utf-8"),
            headers={"Authorization": "Bearer %s" % self.jeton,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as rep:
            return json.loads(rep.read().decode("utf-8") or "{}")

    def call(self, methode: str, chemin: str, corps: dict) -> dict:
        return self._request(methode, chemin, corps)

    def read(self, chemin: str) -> dict:
        """Lecture (GET) : checks d'une tête, exécution, jobs d'une exécution."""
        return self._request("GET", chemin)

    def check_runs(self, tete: str) -> list[dict]:
        """Tous les checks d'une tête (paginés)."""
        checks, page = [], 1
        while True:
            rep = self.read("commits/%s/check-runs?per_page=100&page=%d" % (tete, page))
            page_checks = rep.get("check_runs") or []
            checks += page_checks
            if not page_checks or len(checks) >= int(rep.get("total_count") or 0):
                return checks
            page += 1

    def run(self, execution: int) -> dict:
        """L'exécution GitHub qui a publié un check (details_url ou check_suite)."""
        return self.read("actions/runs/%d" % execution)

    def runs_of_suite(self, suite: int) -> list[dict]:
        """Les exécutions d'une suite de checks (#856).

        GitHub laisse le `details_url` par défaut `…/runs/<id_du_check>` sur
        les checks publiés par un jeton d'exécution : il ne nomme alors pas
        l'exécution, mais le `check_suite` du check la retrouve exactement."""
        return self._listed("actions/runs?check_suite_id=%d" % suite,
                            "workflow_runs", maximum=1000)

    def _listed(self, path: str, key: str, *, maximum: int | None = None) -> list[dict]:
        """Une liste complète et stable, sinon une erreur (pas une liste vide)."""
        items, page, total = [], 1, None
        separator = "&" if "?" in path else "?"
        while True:
            response = self.read("%s%sper_page=100&page=%d" % (path, separator, page))
            page_items = response.get(key)
            count = response.get("total_count")
            if (not isinstance(page_items, list) or type(count) is not int
                    or count < 0 or (maximum is not None and count > maximum)
                    or (total is not None and count != total)
                    or any(not isinstance(item, dict) for item in page_items)):
                raise ValueError("liste %s incomplète" % key)
            total = count
            items.extend(page_items)
            if len(items) == total:
                return items
            if not page_items or len(items) > total:
                raise ValueError("page de %s manquante" % key)
            page += 1

    def jobs(self, execution: int) -> list[dict]:
        """Les jobs d'une exécution : leur état terminal fait foi (#856)."""
        return self._listed("actions/runs/%d/jobs" % execution, "jobs")

    def create(self, nom: str, output: dict) -> int:
        corps = {"name": nom, "head_sha": self.tete, "status": "in_progress", "output": output}
        if self.lien:
            corps["details_url"] = self.lien
        return int(self.call("POST", "check-runs", corps)["id"])

    def update(self, ident: int, output: dict, conclusion: str | None = None) -> None:
        corps: dict = {"output": output}
        if conclusion:
            corps.update(status="completed", conclusion=conclusion)
        self.call("PATCH", "check-runs/%d" % ident, corps)


class Publication:
    """L'état d'une publication, partagé entre `publish` et `close` par un
    fichier à côté du flux (`F.jsonl.check`)."""

    def __init__(self, flux: str, nom: str, gh: GitHub, log=None):
        self.flux, self.nom, self.gh = pathlib.Path(flux), nom, gh
        self.etat_chemin = pathlib.Path(str(flux) + ".check")
        self.log = log or (lambda texte: print(texte, file=sys.stderr, flush=True))
        try:
            self.etat = json.loads(self.etat_chemin.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.etat = {}

    def _keep(self) -> None:
        tmp = self.etat_chemin.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.etat), encoding="utf-8")
        tmp.replace(self.etat_chemin)

    def events(self) -> list[dict]:
        return read(self.flux)[0] if self.flux.is_file() else []

    def push(self, events: list[dict], conclusion: str | None = None) -> bool:
        """Crée ou met à jour le check run ; False si GitHub refuse."""
        output = check_output(events)
        echecs = [e for e in events if e.get("type") == "test"
                  and e.get("statut") in FAILURES]
        vus = set(self.etat.get("annotes") or ())
        nouveaux = [e for e in echecs if e.get("test") not in vus][:MAX_ANNOTATIONS]
        if nouveaux:
            output["annotations"] = annotations(nouveaux)
        try:
            if not self.etat.get("id"):
                self.etat["id"] = self.gh.create(self.nom, output)
                if conclusion:
                    self.gh.update(self.etat["id"], check_output(events), conclusion)
            else:
                self.gh.update(int(self.etat["id"]), output, conclusion)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            self.log("ci-flow : check run « %s » non publié (%s)" % (self.nom, exc))
            return False
        self.etat["annotes"] = sorted(vus | {e.get("test") for e in nouveaux})
        self.etat["echecs"] = len(echecs)
        if conclusion:
            self.etat["conclusion"] = conclusion
        self._keep()
        return True


def conclusion_of(events: list[dict], defaut: str = "cancelled") -> str | None:
    fin = next((e for e in events if e.get("type") == "fin"), None)
    return CONCLUSIONS.get(fin.get("statut"), "failure") if fin else defaut


def publish(args, gh: GitHub | None = None, dormir=time.sleep, horloge=time.monotonic) -> int:
    """Suit le flux et tient le check run jusqu'à la ligne `fin`."""
    gh = gh or GitHub()
    if not gh.ready():
        print("ci-flow : pas de jeton, de dépôt ou de tête : rien n'est publié", file=sys.stderr)
        return 0
    pub = Publication(args.file, args.name, gh)
    limite = horloge() + args.max_duration
    if not pub.push([]):
        return 0  # refus (jeton en lecture seule) : on n'insiste pas
    publies, dernier = (0, 0), horloge()
    while horloge() < limite:
        if pub.etat_chemin.with_suffix(".stop").exists():
            return 0
        events = pub.events()
        tests = [e for e in events if e.get("type") == "test"]
        echecs = sum(1 for e in tests if e.get("statut") in FAILURES)
        fin = conclusion_of(events, defaut=None)
        if fin:
            pub.push(events, fin)
            return 0
        # le check run change à chaque échec ; sinon, toutes les minutes au
        # plus (avancement), pour ménager le quota de l'API
        if echecs != publies[0] or (len(tests) != publies[1] and horloge() - dernier > 60):
            if pub.push(events):
                publies, dernier = (echecs, len(tests)), horloge()
        dormir(args.interval)
    # arrêt de sûreté sans ligne `fin` : le publisher vit encore, le check
    # reste ouvert — `close` (job vivant) ou `sweep` (job terminal) conclura #856
    print("ci-flow : arrêt de sûreté après %.0f s sans ligne fin — check « %s » "
          "laissé ouvert" % (args.max_duration, args.name), file=sys.stderr)
    return 0


def close(args, gh: GitHub | None = None) -> int:
    """Termine le check run (le crée au besoin) avec le bilan de la part."""
    gh = gh or GitHub()
    if not gh.ready():
        return 0
    pub = Publication(args.file, args.name, gh)
    try:
        pub.etat_chemin.with_suffix(".stop").touch()
    except OSError:
        pass
    if pub.etat.get("conclusion"):
        return 0
    events = pub.events()
    pub.push(events, conclusion_of(events))
    return 0


def _execution_of(check: dict) -> int | None:
    """Le numéro d'exécution lu dans le `details_url`, quand il le nomme.

    Les checks publiés par un jeton d'exécution portent l'URL par défaut
    `…/runs/<id_du_check>`, qui ne nomme pas l'exécution : c'est alors le
    `check_suite` qui la retrouve (`_executions_of`)."""
    trouve = re.search(r"/actions/runs/(\d+)", check.get("details_url") or "")
    return int(trouve.group(1)) if trouve else None


def _suite_of(check: dict) -> int | None:
    """Le `check_suite` du check : l'exécution s'y retrouve par l'API (#856)."""
    ident = (check.get("check_suite") or {}).get("id")
    try:
        return int(ident) if ident is not None else None
    except (TypeError, ValueError):
        return None


def _executions_of(check: dict, gh: GitHub, suites: dict[int, list[int]]) -> list[int]:
    """Les exécutions qui ont pu publier le check (#856).

    Celle du `details_url` quand il porte `/actions/runs/<id>` ; sinon celles
    de son `check_suite`. Une liste vide veut dire « non identifiable », jamais
    « orphelin » : sans exécution prouvée terminale, on ne conclut pas."""
    execution = _execution_of(check)
    if execution is not None:
        return [execution]
    suite = _suite_of(check)
    if suite is None:
        return []
    if suite not in suites:
        suites[suite] = [int(run["id"]) for run in gh.runs_of_suite(suite)]
    return suites[suite]


def orphaned(check: dict, jobs: list[dict], execution_status: str) -> bool:
    """Le check « tests (flux) » est-il orphelin ? (#856)

    Il l'est quand il est encore `in_progress` et que le job qui l'a publié est
    terminal : le publisher est mort avec le runner (saturation #747) — ou il a
    expiré sans lire de ligne `fin` — et l'étape `close` n'a pas tourné. Le job
    est retrouvé par son nom (`tests (flux) X` pour le job `X`) ; s'il a
    disparu, l'exécution terminale fait foi. Un job encore actif — y compris
    celui de la tentative courante d'un rerun du même run, qui porte le même
    nom — et une autre tête ne sont jamais touchés."""
    if check.get("status") != "in_progress":
        return False
    nom = check.get("name") or ""
    if not nom.startswith(FLUX_PREFIX):
        return False
    job = next((j for j in jobs if j.get("name") == nom[len(FLUX_PREFIX):]), None)
    if job is not None:
        return job.get("status") == "completed"
    return execution_status == "completed"


def _proved_terminal(check: dict, executions: list[int], gh: GitHub,
                     etats: dict[int, tuple[dict, list[dict]] | None]) -> bool:
    """Le publisher est-il mort dans *toutes* les exécutions candidates ? (#856)

    Le job homonyme doit être terminal dans chaque candidate, même si d'autres
    jobs de l'exécution tournent encore. Si ce job est absent d'une liste lue
    complètement, seule une exécution terminale fait foi. Une réponse illisible
    ou une liste incomplète ne permet jamais de conclure."""
    for execution in executions:
        if execution not in etats:
            try:
                run, jobs = gh.run(execution), gh.jobs(execution)
                if (not isinstance(run, dict) or not isinstance(run.get("status"), str)
                        or not run["status"] or not isinstance(jobs, list)
                        or any(not isinstance(job, dict) for job in jobs)):
                    raise ValueError("réponse d'exécution ou de jobs illisible")
                etats[execution] = (run, jobs)
            except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                print("ci-flow : exécution %d illisible (%s)" % (execution, exc),
                      file=sys.stderr)
                etats[execution] = None
        etat = etats[execution]
        if etat is None or not orphaned(check, etat[1], etat[0].get("status", "")):
            return False
    return True


def sweep(args, gh: GitHub | None = None) -> int:
    """Conclut les checks « tests (flux) » laissés en cours par un job terminal.

    Réconciliation indépendante du publisher (#856) : chaque check encore en
    cours est jugé sur l'état terminal réel du job et de l'exécution qui l'ont
    publié (retrouvée par son `details_url` ou, à défaut, par son
    `check_suite`). Sans ligne `fin`, la conclusion est `cancelled`, jamais
    `success`. Ne ferme aucun check dont l'exécution est encore active, ni ceux
    d'une autre tête. Ne fait jamais échouer le job : une erreur d'API est
    tracée et la fonction rend 0."""
    try:
        return _sweep(args, gh)
    except Exception as exc:  # la réconciliation n'échoue jamais le job
        print("ci-flow : réconciliation interrompue (%s)" % exc, file=sys.stderr)
        return 0


def _sweep(args, gh: GitHub | None = None) -> int:
    gh = gh or GitHub()
    if not gh.ready():
        print("ci-flow : pas de jeton, de dépôt ou de tête : rien à réconcilier",
              file=sys.stderr)
        return 0
    tete = getattr(args, "sha", "") or gh.tete
    try:
        checks = gh.check_runs(tete)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        print("ci-flow : checks de %s illisibles (%s)" % (tete[:8], exc), file=sys.stderr)
        return 0
    etats: dict[int, tuple[dict, list[dict]] | None] = {}
    suites: dict[int, list[int]] = {}
    for check in checks:
        nom = check.get("name") or ""
        if check.get("status") != "in_progress" or not nom.startswith(FLUX_PREFIX):
            continue
        try:
            executions = _executions_of(check, gh, suites)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            print("ci-flow : exécution du check %s illisible (%s)"
                  % (check.get("id"), exc), file=sys.stderr)
            continue
        if not executions:
            # publié hors CI, ni details_url ni check_suite exploitables : on
            # ne juge pas (#856)
            print("ci-flow : check %s « %s » sans exécution identifiable : non conclu"
                  % (check.get("id"), nom), file=sys.stderr)
            continue
        if not _proved_terminal(check, executions, gh, etats):
            continue
        sortie = check.get("output") or {}
        resume = (sortie.get("summary") or "").rstrip()
        sortie = {
            "title": sortie.get("title") or "tests (flux) : check réconcilié (#856)",
            "summary": (resume + "\n\nCheck conclu par réconciliation (#856) : le job "
                        "d'origine est terminal et aucune ligne `fin` n'a été lue — "
                        "conclusion `cancelled`, jamais `success`.").strip(),
            "text": sortie.get("text") or "",
        }
        try:
            gh.update(int(check["id"]), sortie, ORPHAN_CONCLUSION)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            print("ci-flow : check %s non conclu (%s)" % (check.get("id"), exc),
                  file=sys.stderr)
            continue
        print("ci-flow : check %s « %s » conclu %s — job terminal sans ligne fin"
              % (check.get("id"), nom, ORPHAN_CONCLUSION), file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sous = ap.add_subparsers(dest="commande", required=True)
    p = sous.add_parser("summary", help="résumé Markdown du job")
    p.add_argument("files", nargs="+")
    for nom, aide in (("publish", "tient le check run pendant la part (arrière-plan)"),
                      ("close", "termine le check run (étape if: always())")):
        p = sous.add_parser(nom, help=aide)
        p.add_argument("file")
        p.add_argument("--name", required=True, help="nom du check run")
        if nom == "publish":
            p.add_argument("--interval", type=float, default=5.0,
                           help="secondes entre deux lectures du flux (défaut 5)")
            p.add_argument("--max-duration", type=float, default=3600.0,
                           help="arrêt de sûreté (secondes, défaut 3600)")
    p = sous.add_parser("sweep", help="conclut les checks « tests (flux) » laissés "
                         "en cours par un job terminal (#856)")
    p.add_argument("--sha", default="",
                   help="tête à réconcilier (défaut : AMEESH_CI_TETE ou GITHUB_SHA)")
    args = ap.parse_args(argv)
    if args.commande == "publish":
        return publish(args)
    if args.commande == "close":
        return close(args)
    if args.commande == "sweep":
        return sweep(args)
    sys.stdout.write(summary(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
