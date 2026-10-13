#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Lecture du flux des résultats de tests (L161b).

    scripts/ci-flow.py summary F.jsonl [F.jsonl…]   >> "$GITHUB_STEP_SUMMARY"
    scripts/ci-flow.py publish F.jsonl --name "tests (flux) psql 1/6" &   (L161c)
    scripts/ci-flow.py close F.jsonl --name "tests (flux) psql 1/6"      (L161c)

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
dans la sortie du check run, entre `MARKER` et la fin du bloc. Chaque ligne
publiée porte l'attempt de sa part (`GITHUB_RUN_ATTEMPT`, #901) : lors d'une
relance (`gh run rerun`), le check run de l'attempt précédent peut être encore
le dernier publié pendant que la nouvelle tentative est en file ; sans cette
marque, `ameesh notify` imputerait ses anciens échecs à la relance. C'est ce
que relève `ameesh notify` (`ameesh.ci`) pour l'envoyer à l'agent du lot. Il
s'arrête à la ligne `fin`. `close` (étape `if: always()`) le termine dans
tous les cas, et le crée s'il n'existe pas encore. Sans jeton, ou si GitHub
refuse (PR d'une fourche : jeton en lecture seule), rien n'est publié et
l'étape reste verte : la publication n'est jamais une cause d'échec.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import pathlib
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


def _failure_line(ev: dict, lignes: int | None, attempt: int) -> str:
    ev = dict(ev)
    # #901 : la provenance (l'attempt qui a publié cette ligne) est écrite avec
    # l'événement ; le relevé enregistre chaque échec sous son attempt et
    # n'impute à une relance que les lignes de l'attempt courant.
    ev["attempt"] = int(attempt)
    if lignes == 0:
        ev.pop("extrait", None)
    elif lignes is not None and ev.get("extrait"):
        coupe = ev["extrait"].splitlines()
        if len(coupe) > lignes:
            ev["extrait"] = "\n".join(coupe[:lignes] + ["… (%d ligne(s) de plus)"
                                                        % (len(coupe) - lignes)])
    return json.dumps(ev, ensure_ascii=False)


def check_output(events: list[dict], attempt: int = 1) -> dict:
    """`output` du check run : titre, résumé, et le bloc des échecs.

    `attempt` est l'attempt GitHub de la part (défaut 1) : il est porté par
    chaque ligne d'échec publiée (#901)."""
    attempt = int(attempt or 1)
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
            ligne = _failure_line(e, garde, attempt)
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
        # #901 : l'attempt de la part (relance `gh run rerun`), publié avec
        # chaque échec pour que le relevé ne relise pas l'attempt précédent.
        try:
            self.attempt = max(1, int(env.get("GITHUB_RUN_ATTEMPT") or 1))
        except (TypeError, ValueError):
            self.attempt = 1
        self.lien = ("%s/%s/actions/runs/%s" % (env.get("GITHUB_SERVER_URL", "https://github.com"),
                                                self.depot, run) if run else None)

    def ready(self) -> bool:
        return bool(self.jeton and self.depot and self.tete)

    def call(self, methode: str, chemin: str, corps: dict) -> dict:
        req = urllib.request.Request(
            "%s/repos/%s/%s" % (self.api, self.depot, chemin), method=methode,
            data=json.dumps(corps).encode("utf-8"),
            headers={"Authorization": "Bearer %s" % self.jeton,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as rep:
            return json.loads(rep.read().decode("utf-8") or "{}")

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
        attempt = int(getattr(self.gh, "attempt", 1) or 1)
        output = check_output(events, attempt)
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
                    self.gh.update(self.etat["id"], check_output(events, attempt), conclusion)
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
    args = ap.parse_args(argv)
    if args.commande == "publish":
        return publish(args)
    if args.commande == "close":
        return close(args)
    sys.stdout.write(summary(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
