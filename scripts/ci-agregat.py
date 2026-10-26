#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#3449 — l'agrégat requis `tests` n'accorde pas un vert sans les parts Linux.

Décision owner #128512 (signée le 23/10/2026, option a) : « Agrégat requis
jamais conclu = preuve absente, donc bloquant […]. Les non-concluants à
l'intérieur d'un agrégat qui a conclu restent non bloquants (§3). »

#3251 avait lu cette décision comme « au moins une part réussie suffit » : un
run annulé où seuls la part macOS (2 min) et l'agrégat concluaient a publié
`tests` SUCCESS alors que les 8 psycopg et les 16 psql étaient annulés (run
40144843565, paquet h-13, fusionné sans suite Linux). #3449 revient au texte
de #128512 : « les parts annulées ou expirées restent bloquantes ». Une
part **Linux planifiée** annulée, expirée, absente ou en échec rougit donc
l'agrégat, même si les 15 autres ont réussi ; seuls les non-concluants
**à l'intérieur d'un test individuel** sont neutres. Le commentaire de
#3251 qui élargissait #128512 est justement le maillon qui a laissé passer
h-13.

Ce script sépare donc trois cas :

* un **échec** (`failure`, ou une part sautée qui devait tourner) est un
  négatif : l'agrégat conclut rouge (code retour 1) ;
* une **famille Linux planifiée** (`psycopg`, `psql` avec au moins une part)
  qui a une part annulée, expirée, absente ou en échec est un manque :
  l'agrégat conclut rouge (code retour 1) et le détail la nomme. Si toutes
  ses parts sont non concluantes et qu'aucune n'a réussi, c'est une preuve
  absente (même rouge) : c'est le cas du run annulé de h-13, macOS vert,
  0/8 psycopg et 0/16 psql réussies. Les jobs du run sont lus par l'API
  (`actions: read`) parce que `needs.psycopg.result` vaut `cancelled` dès
  qu'une part l'est, sans dire combien ont réussi ;
* une **annulation d'une part non requise** (macOS, autre système de
  fichiers) reste un non concluant : l'agrégat conclut vert s'il garde la
  preuve Linux, et publie le check de détail en `neutral`, avec la part et
  son état. `fire` et `radar` le consignent (lot de correction #3209) sans
  retenir le paquet ni la release — canon §3. Le détail est aussi écrit en
  annotation `::warning::`, jamais muet. Si **aucune** part due n'a réussi
  (toutes annulées ou sautées), c'est l'absence de toute preuve : l'agrégat
  rougit (décision #128512, #940).
* un **run annulé** ne conclut pas : l'étape `if: cancelled()` du workflow
  échoue explicitement avant le bilan, et une famille Linux sans part
  réussie est vue par la classification (`needs.*.result` et jobs du run),
  pas seulement par cette garde — un job `always()` démarré après
  l'annulation du run ne voit pas toujours `cancelled()` (#3449, run
  40144843565).

Le check de détail est publié à **chaque** exécution (vert quand toutes les
parts ont conclu) : une tentative ultérieure sur la même tête remplace ainsi
l'ancien `neutral` au lieu de laisser une consignation périmée
(`radar.latest_rollup` garde le dernier essai par nom).

L'agrégat lui-même ne publie jamais `success` sans ce détail : si le check
ne peut pas être publié alors qu'une annulation doit être consignée (jeton
en lecture seule d'une PR de fourche, API en panne), l'étape échoue (code
retour 2), et le check requis reste rouge. Une annulation ne devient donc
jamais un vert silencieux.

Usage (étape « bilan des parts » de .github/workflows/tests.yml) :

    scripts/ci-agregat.py --event pull_request --run-id 123 --plan success \
        --version success --version-only false --docs-only false \
        --doc-readers skipped \
        --psycopg success --psycopg-parts '[1, 2]' \
        --psql success --psql-parts '[1]' \
        --plateforme-macos cancelled --autre-fs skipped --publish

Sans `--publish` (tests locaux), la classification seule est rendue.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

#: nom du check de détail, partagé avec `ameesh.radar.AGGREGATE_DETAIL_CHECK`
#: (un témoin de tests/test_3251_agregat_annulation.py verrouille l'égalité).
DETAIL_CHECK = "tests (bilan des parts)"

#: conclusions de job qui ne prouvent rien (canon §3 : non concluant)
NON_CONCLUSIVE = ("cancelled", "timed_out", "startup_failure", "stale",
                  "action_required")

#: état d'une part
GREEN = "green"
RED = "red"
MISSING = "missing"
INCONCLUSIVE = "inconclusive"


class AgregatError(RuntimeError):
    """Lecture impossible ou publication refusée : l'agrégat ne se suppose
    pas vert."""


def _state(value) -> str:
    """La conclusion d'un job, normalisée (casse libre, valeur absente)."""
    return str(value or "").strip().lower()


def _flag(value) -> bool:
    """Un booléen de `needs.plan.outputs` (`true`/`false`, casse libre)."""
    return str(value or "").strip().lower() == "true"


def _json_list(text) -> list | None:
    """La liste de parts d'un plan (`[1, 2]`), ou None si elle est illisible."""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, list) else None


def _family_jobs(jobs, family: str) -> list:
    """Les jobs d'une famille (`psycopg 1/8`, `psql 1/16`) parmi les jobs lus.

    Appelée par `paquet.yml`, `tests.yml` est un workflow réutilisable : l'API
    préfixe le nom de chaque job appelé par celui du job appelant
    (`suite complète du paquet / psycopg 1/8`). On lit donc la feuille après
    le dernier ` / `, sans dépendre du préfixe."""
    prefix = family + " "
    return [job for job in (jobs or []) if isinstance(job, dict)
            and str(job.get("name", "")).rsplit(" / ", 1)[-1].startswith(prefix)]


def _job_stats(jobs, expected):
    """Les comptes d'une famille, ou None si ses jobs sont illisibles.

    `jobs` est la liste des jobs du run (`name`, `status`, `conclusion`),
    `expected` le nombre de parts du plan (les jobs jamais démarrés ne sont
    pas toujours listés). Une liste vide est une lecture valide : zéro part
    réussie, toutes les parts planifiées absentes. Tout job dont la
    conclusion n'est ni `success` ni non concluante (`cancelled`,
    `timed_out`…) est un échec individuel : une famille `cancelled` qui cache
    un `failure` ne doit jamais verdir (B2 de cx3/dev1). Une part planifiée
    absente de la lecture n'est pas une preuve non plus (B3 de cx2), et une
    part annulée ou expirée reste bloquante (prémisse de #128512, pilote
    25/10 01:55)."""
    if jobs is None:
        return None
    rows = [job for job in jobs if isinstance(job, dict)]
    conclusions = [_state(job.get("conclusion")) for job in rows]
    successes = sum(1 for value in conclusions if value == "success")
    failures = sum(1 for value in conclusions
                   if value != "success" and value not in NON_CONCLUSIVE)
    nonconclusive = sum(1 for value in conclusions if value in NON_CONCLUSIVE)
    planned = int(expected or 0)
    total = max(planned, len(rows))
    absent = max(0, planned - len(rows))
    return {"success": successes, "total": total, "failure": failures,
            "absent": absent, "nonconclusive": nonconclusive}


def classify(*, event, plan, version, version_only, docs_only, doc_readers,
             psycopg, psycopg_parts, psql, psql_parts,
             platform_macos=None, fs_class=None,
             psycopg_jobs=None, psql_jobs=None, psql_full=False) -> dict:
    """Classe l'agrégat sans réseau : `{state, red, missing, consigned, parts}`.

    `state` vaut `green` quand la preuve Linux est acquise (chaque famille
    planifiée garde au moins une part réussie), `red` sur un négatif ou une
    preuve absente. `red` liste les négatifs prouvés, `missing` les familles
    Linux planifiées sans aucune part réussie (et l'absence de toute part
    réussie), `consigned` les annulations des parts **non requises** (macOS,
    autre système de fichiers, dans l'ordre du workflow), `parts` le détail
    de chaque part exigée (pour le check publié).

    `psycopg_jobs` / `psql_jobs` sont les jobs du run lus par l'API : une
    part Linux planifiée annulée, expirée, absente ou en échec rougit la
    famille, même si les 15 autres ont réussi (15/16 = rouge, #128512).
    Sans jobs lisibles, une famille non concluante est une preuve absente
    (jamais un vert sur une lecture manquante).

    `psql_full` (#3749) : le nocturne psql (`psql-nocturne.yml`, entrée
    `psql-full` de tests.yml) ne planifie aucune part psycopg ; c'est le seul
    cas où une famille psycopg vide (`[]`) et sautée est légitime."""
    red: list[str] = []
    missing: list[str] = []
    consigned: list[dict] = []
    parts: list[dict] = []

    def add(name: str, result, parts_text: str, *, skipped_ok: bool,
            required: bool, jobs=None, expected=None) -> None:
        value = _state(result)
        row = {"part": name, "result": value or "absent", "parts": parts_text}
        if value == "success":
            row["status"] = GREEN
        elif value in NON_CONCLUSIVE:
            stats = _job_stats(jobs, expected) if required else None
            if stats is not None:
                row["success"] = stats["success"]
            if not required:
                row["status"] = INCONCLUSIVE
                consigned.append(row)
            elif stats is None:
                # Jobs illisibles : jamais un vert sur une lecture manquante.
                row["status"] = MISSING
                missing.append("%s (%s, preuve absente : jobs du run "
                               "illisibles)" % (name, value))
            elif stats["failure"] or stats["absent"] or stats["nonconclusive"]:
                # B2/B3 et #128512 : un échec individuel, une part planifiée
                # absente, ou une part planifiée annulée/expirée restent
                # bloquants. Toutes les parts non concluantes et aucune
                # réussie = preuve absente (cas h-13).
                gaps = []
                if stats["failure"]:
                    gaps.append("%d/%d part(s) en échec"
                                % (stats["failure"], stats["total"]))
                if stats["absent"]:
                    gaps.append("%d/%d part(s) planifiée(s) absente(s)"
                                % (stats["absent"], stats["total"]))
                if stats["nonconclusive"]:
                    gaps.append("%d/%d part(s) annulée(s) ou non concluante(s)"
                                % (stats["nonconclusive"], stats["total"]))
                if (stats["success"] == 0 and not stats["failure"]
                        and not stats["absent"]):
                    row["status"] = MISSING
                    missing.append("%s (%s, 0/%d part(s) réussie(s), preuve "
                                   "absente)" % (name, value, stats["total"]))
                else:
                    row["status"] = RED
                    red.append("%s (%s, famille %s)"
                               % (name, ", ".join(gaps), value))
            else:
                # Toutes les parts lues sont vertes, mais la famille ne
                # conclut pas (run annulé après les parts) : pas un vert.
                row["status"] = RED
                red.append("%s (%s, famille non concluante malgré %d/%d "
                           "part(s) verte(s))"
                           % (name, value, stats["success"], stats["total"]))
        elif value == "skipped" and skipped_ok:
            # un saut prévu par le plan n'a rien à consigner
            row["status"] = GREEN
        else:
            row["status"] = RED
            detail = value or "absent"
            if value == "skipped" and parts_text:
                detail = "skipped, parts %s" % parts_text
            red.append("%s (%s)" % (name, detail))
        parts.append(row)

    event = _state(event)
    plan_state = _state(plan)
    if plan_state != "success":
        red.append("plan (%s)" % (plan_state or "absent"))
    version_state = _state(version)
    if event == "pull_request":
        if version_state != "success":
            red.append("version (%s)" % (version_state or "absent"))
    elif version_state not in ("success", "skipped"):
        red.append("version (%s)" % (version_state or "absent"))
    if event == "pull_request":
        # #1343 : une demande de gel ne prend jamais les voies version seule
        # (L157) ou documentation seule (L161a) — un `tests` vert sans tests.
        if _flag(version_only):
            red.append("version_only (voie interdite sur une demande de gel)")
        if _flag(docs_only):
            red.append("docs_only (voie interdite sur une demande de gel)")

    # L157 : une PR qui ne change que le numéro de version a déjà été validée
    # sur main ; aucune part ne tourne. L161a : ses lecteurs déclarés seulement.
    if _flag(version_only) and not red:
        return {"state": GREEN, "red": [], "missing": [], "consigned": [],
                "parts": []}
    if _flag(docs_only):
        readers = _state(doc_readers)
        if readers != "success":
            red.append("doc-readers (%s)" % (readers or "absent"))
        if not red:
            return {"state": GREEN, "red": [], "missing": [], "consigned": [],
                    "parts": []}

    # #3449 : psycopg est toujours planifiée (les voies version/doc sont
    # sorties plus haut) ; un job sauté, absent ou sans part réussie y est un
    # manque. psql n'est requise que si le plan sélectionne des parts : une
    # liste explicitement vide (`[]`) est son seul saut légitime. #3749 : le
    # nocturne psql est la seule exception côté psycopg, et seulement avec une
    # liste explicitement vide.
    psycopg_expected = len(_json_list(psycopg_parts) or [])
    psql_expected = len(_json_list(psql_parts) or [])
    psycopg_off = _flag(psql_full) and _json_list(psycopg_parts) == []
    add("psycopg", psycopg, str(psycopg_parts or "").strip(),
        skipped_ok=psycopg_off, required=not psycopg_off,
        jobs=psycopg_jobs, expected=psycopg_expected)
    add("psql", psql, str(psql_parts or "").strip(),
        skipped_ok=(_json_list(psql_parts) == []),
        required=(_json_list(psql_parts) != []),
        jobs=psql_jobs, expected=psql_expected)
    if platform_macos is not None:
        # L68b (#3026) : le job court macOS entre dans les needs de `tests` ;
        # son annulation reste une annulation interne : la preuve Linux suffit
        # (#3449).
        add("plateforme macOS", platform_macos, "", skipped_ok=False,
            required=False)
    if fs_class is not None:
        # #748 : la part « autre système de fichiers » ne tourne qu'à la
        # release (ou à la main) ; sautée ailleurs, elle n'a rien à prouver.
        add("autre système de fichiers", fs_class, "", skipped_ok=True,
            required=False)

    # Décision #128512 : « agrégat requis jamais conclu = preuve absente, donc
    # bloquant ». #3449 : une famille Linux sans part réussie est déjà une
    # preuve absente ci-dessus ; ici on ferme le cas où **aucune** part due
    # n'a réussi (toutes annulées ou sautées), y compris si seules des parts
    # non requises ont conclu — #940 reste fermé (jamais de tests SUCCESS
    # sans test).
    if parts and not any(
            (row["status"] == GREEN and row["result"] == "success")
            or row.get("success", 0) > 0 for row in parts):
        missing.append("aucune part n'a conclu (preuve absente)")

    return {"state": RED if red or missing else GREEN, "red": red,
            "missing": missing, "consigned": consigned, "parts": parts}


def _summary(event: str, parts: list[dict], result: dict) -> str:
    lines = ["Agrégat requis **tests** — événement : %s." % (event or "?")]

    def display(row: dict) -> str:
        suffix = " (parts %s)" % row["parts"] if row.get("parts") else ""
        return "- %s : %s%s" % (row["part"], row["result"], suffix)

    lines += [display(row) for row in parts]
    if result["consigned"]:
        lines.append("")
        lines.append("Non concluant (non bloquant, canon §3 ; lot de correction "
                     "ouvert par fire/radar) : %s."
                     % ", ".join(row["part"] for row in result["consigned"]))
    if result["missing"]:
        lines.append("")
        lines.append("Preuve absente (bloquant, décision #128512, #3449) : %s."
                     % ", ".join(result["missing"]))
    if result["red"]:
        lines.append("")
        lines.append("Négatif prouvé (bloquant) : %s." % ", ".join(result["red"]))
    return "\n".join(lines)


class GitHub:
    """La publication du check de détail, avec le jeton du job (aucun `gh`)."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.jeton = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or ""
        self.depot = env.get("GITHUB_REPOSITORY", "")
        self.tete = env.get("AMEESH_CI_TETE") or env.get("GITHUB_SHA", "")
        self.api = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.execution = env.get("GITHUB_RUN_ID", "")
        self.tentative = env.get("GITHUB_RUN_ATTEMPT", "")
        self.serveur = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")

    def ready(self) -> bool:
        return bool(self.jeton and self.depot and self.tete)

    def jobs(self, run_id) -> list[dict]:
        """Les jobs du run (parts comprises), lus par l'API Actions.

        `actions: read` est accordé au job `tests` ; une lecture impossible ou
        tronquée lève `AgregatError` — le classifieur retombe alors sur une
        preuve absente pour une famille annulée, jamais un vert muet."""
        if not self.ready():
            raise AgregatError("no token, repository or head: the run jobs "
                              "cannot be read")
        if not str(run_id).isdigit() or int(run_id) <= 0:
            raise AgregatError("run jobs: invalid run id %r" % (run_id,))
        request = urllib.request.Request(
            "%s/repos/%s/actions/runs/%s/jobs?per_page=100&filter=latest"
            % (self.api, self.depot, int(run_id)),
            headers={"Authorization": "Bearer %s" % self.jeton,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise AgregatError("run jobs: HTTP %d %s" % (exc.code, detail)) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise AgregatError("run jobs: %s" % exc) from exc
        if (not isinstance(body, dict)
                or type(body.get("total_count")) is not int
                or not isinstance(body.get("jobs"), list)
                or body["total_count"] != len(body["jobs"])
                or body["total_count"] >= 100):
            raise AgregatError("run jobs: malformed, truncated or wrong response")
        rows = []
        for job in body["jobs"]:
            if (not isinstance(job, dict) or not isinstance(job.get("name"), str)
                    or not job["name"]):
                raise AgregatError("run jobs: invalid job")
            rows.append({"name": job["name"],
                         "status": job.get("status") or "",
                         "conclusion": job.get("conclusion") or ""})
        return rows

    def create(self, name: str, conclusion: str, title: str, summary: str) -> int:
        """Publie le check de détail sur la tête poussée ; l'identifiant rendu."""
        if not self.ready():
            raise AgregatError("no token, repository or head: the detail check "
                              "cannot be published")
        payload = {"name": name, "head_sha": self.tete, "status": "completed",
                   "conclusion": conclusion,
                   "output": {"title": title[:255], "summary": summary[:60000]}}
        if self.execution:
            payload["details_url"] = "%s/%s/actions/runs/%s" % (
                self.serveur, self.depot, self.execution)
            payload["external_id"] = "ameesh-ci-agregat/%s/%s" % (
                self.execution, self.tentative or "1")
        request = urllib.request.Request(
            "%s/repos/%s/check-runs" % (self.api, self.depot),
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer %s" % self.jeton,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise AgregatError("check-runs: HTTP %d %s" % (exc.code, detail)) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise AgregatError("check-runs: %s" % exc) from exc
        if not isinstance(body, dict) or not isinstance(body.get("id"), int):
            raise AgregatError("check-runs: unreadable response")
        return body["id"]


def _parse(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="bilan des parts de l'agrégat requis « tests » "
                    "(#3251, #3449)")
    parser.add_argument("--event", default="")
    parser.add_argument("--plan", default="")
    parser.add_argument("--version", default="")
    parser.add_argument("--version-only", default="false")
    parser.add_argument("--docs-only", default="false")
    parser.add_argument("--doc-readers", default="")
    parser.add_argument("--psycopg", default="")
    parser.add_argument("--psycopg-parts", default="")
    parser.add_argument("--psql", default="")
    parser.add_argument("--psql-parts", default="")
    parser.add_argument("--psql-full", default="false",
                        help="nocturne psql (#3749) : psycopg sans part admise")
    parser.add_argument("--plateforme-macos", dest="platform_macos", default=None)
    parser.add_argument("--autre-fs", dest="fs_class", default=None)
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""),
                        help="run dont lire les jobs (défaut : GITHUB_RUN_ID)")
    parser.add_argument("--publish", action="store_true",
                        help="publier le check de détail sur la tête poussée")
    return parser.parse_args(argv)


def main(argv=None, gh=None, jobs=None) -> int:
    args = _parse(argv)
    if jobs is None and args.run_id:
        try:
            jobs = (gh or GitHub()).jobs(args.run_id)
        except AgregatError as exc:
            print("ci-agregat : jobs du run illisibles : %s" % exc, file=sys.stderr)
    psycopg_jobs = _family_jobs(jobs, "psycopg") if jobs is not None else None
    psql_jobs = _family_jobs(jobs, "psql") if jobs is not None else None
    try:
        result = classify(
            event=args.event, plan=args.plan, version=args.version,
            version_only=args.version_only, docs_only=args.docs_only,
            doc_readers=args.doc_readers,
            psycopg=args.psycopg, psycopg_parts=args.psycopg_parts,
            psql=args.psql, psql_parts=args.psql_parts,
            platform_macos=args.platform_macos, fs_class=args.fs_class,
            psycopg_jobs=psycopg_jobs, psql_jobs=psql_jobs,
            psql_full=args.psql_full)
    except (TypeError, ValueError) as exc:  # pragma: no cover - garde-fou
        print("ci-agregat : %s" % exc, file=sys.stderr)
        return 2

    for row in result["parts"]:
        suffix = " (parts %s)" % row["parts"] if row.get("parts") else ""
        print("%-24s: %s%s" % (row["part"], row["result"], suffix))
    for gap in result["missing"]:
        print("::error title=tests : preuve absente::%s" % gap)
    for gap in result["red"]:
        print("::error title=tests : négatif prouvé::%s" % gap)
    for row in result["consigned"]:
        print("::warning title=tests : part non concluante::%s a été %s%s ; "
              "consignée, non bloquante (canon §3, décision #128512)."
              % (row["part"], row["result"],
                 " (parts %s)" % row["parts"] if row.get("parts") else ""))

    conclusion = ("failure" if result["state"] == RED
                  else "neutral" if result["consigned"] else "success")
    if result["state"] == RED:
        title = ("tests : preuve absente" if result["missing"]
                 else "tests : négatif prouvé")
    elif result["consigned"]:
        title = "tests : %d part(s) non concluante(s)" % len(result["consigned"])
    else:
        title = "tests : toutes les parts ont conclu"
    summary = _summary(args.event, result["parts"], result)

    if args.publish:
        try:
            (gh or GitHub()).create(DETAIL_CHECK, conclusion, title, summary)
        except AgregatError as exc:
            print("ci-agregat : %s" % exc, file=sys.stderr)
            # Une annulation qui ne peut pas être consignée ne verdit jamais :
            # ce serait rouvrir #940 (un `tests` SUCCESS muet). Un vert complet
            # reste vert (une PR de fourche n'a pas `checks: write`).
            if result["consigned"] or result["state"] == RED:
                return 2
            print("::warning title=tests : détail non publié::%s" % exc)
    return 0 if result["state"] == GREEN else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
