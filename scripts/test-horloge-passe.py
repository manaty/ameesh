#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#5427 : la passe horloge et fuseau, hors de la porte de fusion.

Sept lots correctifs en deux semaines, trois paquets rouges en 24 h : des
tests dépendaient du jour ou de l'horloge du runner, et le défaut ne se
voyait qu'en CI de paquet, une fois le paquet composé (#2600, #4168, #5086,
#5302, #5314, #5321, #5412). Cette passe rejoue les tests sensibles au jour
**avant** la porte de fusion, sous une horloge et un fuseau contraints :

  23h45  libfaketime à 23:45 UTC (TZ=UTC) : le jour bascule 15 min plus tard ;
  00h05  libfaketime à 00:05 UTC (TZ=UTC) : le jour vient de basculer ;
         ces deux scénarios ne tournent que dans les 15 min autour de leur
         cible (au-delà, la base reste à l'heure réelle : baux et âges
         divergeraient) ; hors fenêtre ils sont « pas dus » ;
  fuseau TZ=Pacific/Kiritimati (UTC+14), sans libfaketime : un jour local
         très en avance sur UTC, comme le fuseau de session SQL.

La sélection suit le scénario : sous horloge simulée, seuls les modules que le
lint d'herméticité (`scripts/test-hermeticity-lint.py`, règle H3) marque
« fixture dépendante du jour » — la graine `tests/parts/horloge.txt` contient
des tests de temps réel et de processus (#2600) que libfaketime déstabilise
sans rien prouver sur le jour. Le scénario de fuseau, qui ne touche pas
l'horloge monotone, rejoue en plus la graine : les modules des lots qui ont
rougi un paquet pour cette raison. `--module` remplace la sélection ; `--all`
prend la suite entière (psycopg) — long, réservé au lancement manuel.

Un rouge n'est jamais un verdict de la porte : la passe vit hors de la CI de
paquet. Selon le canon §3, un test qui dépend de l'environnement est un défaut
du test, corrigé à la source ; la passe nomme les tests en échec et le rouge
ouvre un lot d'admission « test dépendant de l'environnement » (le workflow
.github/workflows/horloge.yml ouvre le ticket, la veille GitHub le convertit
en lot dans l'heure). Une passe non concluante (libfaketime absent, horloge
simulée non vérifiée, test qui ne rend pas la main) ne bloque rien : elle est
consignée et rejouée.

Usage :
  scripts/test-horloge-passe.py [--scenario 23h45|00h05|fuseau] [--module M]
                                [--json RAPPORT] [--self-check] [--list]
  scripts/test-horloge-passe.py --all --scenario fuseau

Sortie : 0 vert, 1 au moins un rouge, 77 non concluant (libfaketime absent,
horloge simulée non vérifiée, délai dépassé sans échec prouvé).
"""
from __future__ import annotations

import argparse
import ctypes.util
import datetime as dt
import glob
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
#: fuseau extrême de la passe ; le même pour la session SQL (`PGTZ`)
EXTREME_TZ = "Pacific/Kiritimati"
#: sortie de test qui nomme un échec ; le nom suffit à ouvrir un lot
FAILURE_RE = re.compile(r"^(?:FAIL|ERROR): (.+)$", re.MULTILINE)
#: durée d'un scénario (secondes) ; l'appelant borne le job entier
DEFAULT_TIMEOUT = 1200
#: la ligne de temps simulé est vérifiée à cette tolérance (secondes)
CLOCK_TOLERANCE = 180
#: écart maximal entre l'heure réelle et l'heure simulée (secondes).
#: Au-delà, la base (restée à l'heure réelle) et le test divergent : baux,
#: âges et `now()` produiraient des rouges qui ne disent rien du jour. La
#: passe est alors **pas due** (jamais rouge) ; la fenêtre de 15 min couvre
#: le retard habituel de GitHub sur un cron et garde le décalage avec la
#: base sous le quart d'heure.
MAX_SKEW_S = 900


class Unavailable(RuntimeError):
    """Le scénario ne peut pas conclure (outil absent, horloge non simulée)."""


class NotDue(Unavailable):
    """Le scénario n'est pas dans sa fenêtre : il n'est pas rejoué."""


def modules_of(path: pathlib.Path) -> list[str]:
    """Les modules d'une liste `tests/parts/*.txt` : un nom `tests.x` par ligne,
    `#` commente, les lignes vides sont ignorées."""
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def lint_calendar_modules(repo: pathlib.Path = REPO) -> list[str]:
    """Les modules que le lint marque H3 pour une fixture au **jour** courant.

    Seul `datetime.date.today()` est rejoué : les fixtures bâties sur
    `datetime.now()` (instant) ne sont pas des bascules de jour, et les
    modules qui les portent (migration des exécuteurs, registre des
    processus) sont des intégrations longues que libfaketime déstabilise sans
    rien prouver sur le calendrier. Le lint les avertit quand même au gel.
    Un lint absent ou illisible ne fait pas tomber la passe : la liste
    `horloge.txt` reste, et l'absence est dite."""
    try:
        proc = subprocess.run(
            [sys.executable, str(repo / "scripts" / "test-hermeticity-lint.py"),
             "--rules", "H3", "--json"],
            cwd=repo, capture_output=True, text=True, timeout=120)
        payload = json.loads(proc.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print("lint d'herméticité illisible (%s) : liste horloge.txt seule" % exc,
              file=sys.stderr)
        return []
    modules = []
    for finding in payload.get("findings") or ():
        path = str(finding.get("path") or "")
        if finding.get("rule") != "H3":
            continue
        if "datetime.date.today()" not in str(finding.get("message") or ""):
            continue
        if path.endswith(".py") and pathlib.PurePath(path).name.startswith("test_"):
            modules.append(path[:-3].replace(os.sep, "."))
    return _dedupe(modules)


def _dedupe(modules: list[str]) -> list[str]:
    out: list[str] = []
    for module in modules:
        if module and module not in out:
            out.append(module)
    return out


def seed_modules(repo: pathlib.Path = REPO) -> list[str]:
    """Les modules des lots qui ont rougi un paquet (liste `horloge.txt`)."""
    return modules_of(repo / "tests" / "parts" / "horloge.txt")


def calendar_modules(repo: pathlib.Path = REPO) -> list[str]:
    """Les modules que le lint marque H3 (fixture dépendante du jour)."""
    return lint_calendar_modules(repo)


def selection(repo: pathlib.Path = REPO) -> list[str]:
    """L'union affichée par `--list` : la graine, puis les constats H3."""
    return _dedupe(seed_modules(repo) + calendar_modules(repo))


def scenario_modules(scenario: dict, *, repo: pathlib.Path = REPO,
                     override: list[str] | None = None,
                     everything: bool = False,
                     seed: list[str] | None = None,
                     calendar: list[str] | None = None) -> list[str]:
    """Les modules d'un scénario.

    L'horloge simulée ne rejoue que les constats H3 (calendrier) : la graine
    contient des tests de temps réel et de processus (`test_runner`, #2600)
    que libfaketime déstabilise sans rien prouver sur le jour. Le fuseau
    extrême, lui, ne touche pas l'horloge monotone : il rejoue la graine
    aussi."""
    if override:
        return _dedupe(override)
    if everything:
        return all_modules(repo)
    calendar = calendar_modules(repo) if calendar is None else calendar
    if scenario.get("fake_at") is not None:
        return _dedupe(calendar)
    seed = seed_modules(repo) if seed is None else seed
    return _dedupe(seed + calendar)


def all_modules(repo: pathlib.Path = REPO) -> list[str]:
    paths = sorted((repo / "tests").glob("test_*.py"))
    return [str(path.relative_to(repo))[:-3].replace(os.sep, ".") for path in paths]


def libfaketime() -> str | None:
    """Le nom ou le chemin de libfaketime pour `LD_PRELOAD`, ou None."""
    explicit = os.environ.get("AMEESH_FAKETIME_LIB")
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    found = ctypes.util.find_library("faketime")
    if found:
        return found
    for pattern in ("/usr/lib/*/faketime/libfaketime.so.1",
                    "/usr/lib/faketime/libfaketime.so.1",
                    "/usr/local/lib/faketime/libfaketime.so.1"):
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


def _clock_env(base: dict, *, fake_at: dt.datetime | None, tz: str) -> dict:
    env = dict(base)
    env["TZ"] = tz
    # libpq transmet le fuseau de session depuis PGTZ : `current_date` et le
    # jour local du processus lisent alors le même calendrier.
    env["PGTZ"] = tz
    if fake_at is not None:
        preload = libfaketime()
        if preload is None:
            raise Unavailable("libfaketime introuvable (paquet faketime)")
        old = env.get("LD_PRELOAD")
        env["LD_PRELOAD"] = preload if not old else preload + ":" + old
        # `@…` (start-at) : l'horloge part de l'instant posé et avance d'elle-
        # même ; le modificateur `x` exigerait un décalage relatif et casse
        # certaines versions de libfaketime. Un test qui attend un timeout
        # réel doit voir le temps passer.
        env["FAKETIME"] = "@%s" % fake_at.strftime("%Y-%m-%d %H:%M:%S")
        # Les gardes de temps (timeouts, attentes) restent réelles : on ne
        # simule que l'horloge murale, pas l'ordonnanceur.
        env["FAKETIME_DONT_FAKE_MONOTONIC"] = "1"
        # `stat` fausse les mtime des fichiers temporaires : inutile ici.
        env["NO_FAKE_STAT"] = "1"
    return env


def _python_env(env: dict, repo: pathlib.Path) -> dict:
    out = dict(env)
    src = str(repo / "src")
    old = out.get("PYTHONPATH")
    out["PYTHONPATH"] = src if not old else src + os.pathsep + old
    # Le `psql` d'Ubuntu est un wrapper Perl (pg_wrapper) : sous libfaketime,
    # son `select` se bloque et le pilote psql de `db.py` ne rend pas la main.
    # La passe utilise psycopg, comme la CI de la suite ; le pilote psql n'est
    # pas ce que la barrière horloge mesure.
    out.setdefault("AMEESH_DRIVER", "psycopg")
    return out


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _nearest(now: dt.datetime, hour: int, minute: int) -> dt.datetime:
    """L'occurrence de `hour:minute` la plus proche de `now` (±12 h)."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target - now > dt.timedelta(hours=12):
        target -= dt.timedelta(days=1)
    elif now - target > dt.timedelta(hours=12):
        target += dt.timedelta(days=1)
    return target


def scenarios(now: dt.datetime | None = None) -> list[dict]:
    """Les trois scénarios, horodatés autour de `now` (UTC, injectable).

    L'occurrence la plus proche de 23:45 et de 00:05, jamais « demain » à
    22 h de distance : la passe tourne près de la bascule, sinon le décalage
    entre l'horloge simulée et celle de la base fausserait les tests de bail
    et d'âge (voir [`MAX_SKEW_S`])."""
    now = now or _utcnow()
    return [
        {"name": "23h45", "kind": "faketime", "tz": "UTC",
         "fake_at": _nearest(now, 23, 45),
         "why": "le jour bascule dans 15 min"},
        {"name": "00h05", "kind": "faketime", "tz": "UTC",
         "fake_at": _nearest(now, 0, 5),
         "why": "le jour vient de basculer"},
        {"name": "fuseau", "kind": "timezone", "tz": EXTREME_TZ,
         "fake_at": None, "why": "jour local très en avance sur UTC"},
    ]


def check_clock(env: dict, target: dt.datetime, repo: pathlib.Path = REPO) -> None:
    """Refuse un scénario dont l'horloge n'est pas celle annoncée.

    libfaketime peut être installé sans agir (binaire non préchargeable,
    `LD_PRELOAD` ignoré) : un test vert sous l'horloge réelle ne prouverait
    rien. On lit l'heure dans un processus fils, comme les tests."""
    code = ("import datetime as d, time\n"
            "n = d.datetime.now(d.timezone.utc)\n"
            "print('%s %s' % (n.strftime('%Y-%m-%d %H:%M'), time.time()))\n")
    try:
        proc = subprocess.run([sys.executable, "-c", code], cwd=repo,
                              env=_python_env(env, repo), capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unavailable("horloge simulée illisible : %s" % exc) from exc
    try:
        parts = proc.stdout.split()
        seen = dt.datetime.strptime(" ".join(parts[:2]), "%Y-%m-%d %H:%M")
        seen = seen.replace(tzinfo=dt.timezone.utc)
        drift = abs(float(parts[2]) - seen.timestamp())
    except (ValueError, IndexError):
        raise Unavailable("horloge simulée illisible : %r" % proc.stdout[:120])
    if proc.returncode != 0 \
            or abs((seen - target).total_seconds()) > CLOCK_TOLERANCE \
            or drift > CLOCK_TOLERANCE:
        raise Unavailable(
            "horloge simulée non vérifiée : attendu %s, lu %r (drift %.0f s)"
            % (target.strftime("%Y-%m-%d %H:%M"), proc.stdout.strip(), drift))


def _kill_group(proc) -> None:
    """Le test et ses descendants (harnais, fils) partent ensemble."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def run_scenario(scenario: dict, modules: list[str], *, repo: pathlib.Path = REPO,
                 base_env: dict | None = None,
                 timeout: int = DEFAULT_TIMEOUT,
                 max_skew: float = MAX_SKEW_S) -> dict:
    """Rejoue `modules` sous un scénario ; rend son compte rendu, sans lever
    pour un rouge (seule une indisponibilité lève [`Unavailable`]).

    La sortie va dans un fichier, jamais dans un tube : un descendant laissé
    par un test (`test_runner` en lance) garderait le tube ouvert et la passe
    attendrait indéfiniment un EOF. Au délai, le groupe entier est tué."""
    if scenario["fake_at"] is not None and max_skew:
        skew = abs((scenario["fake_at"] - _utcnow()).total_seconds())
        if skew > max_skew:
            raise NotDue(
                "horloge simulée à %s hors fenêtre (écart %.0f s > %.0f s)"
                % (scenario["fake_at"].strftime("%Y-%m-%d %H:%M"), skew, max_skew))
    env = _python_env(_clock_env(base_env if base_env is not None else os.environ,
                                 fake_at=scenario["fake_at"], tz=scenario["tz"]),
                      repo)
    if scenario["fake_at"] is not None:
        check_clock(env, scenario["fake_at"], repo)
    cmd = [sys.executable, "-m", "unittest", "-q", *modules]
    start = time.monotonic()
    timed_out = False
    with tempfile.TemporaryFile() as sink:
        proc = subprocess.Popen(cmd, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                stdout=sink, stderr=subprocess.STDOUT,
                                start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            code, timed_out = None, True
        sink.seek(0)
        output = sink.read().decode("utf-8", "replace")
    if timed_out:
        output += "\nne rend pas la main en %d s" % timeout
    failures = [line.strip() for line in FAILURE_RE.findall(output)]
    return {"name": scenario["name"], "kind": scenario["kind"],
            "tz": scenario["tz"], "why": scenario["why"],
            "fake_at": (scenario["fake_at"].isoformat()
                        if scenario["fake_at"] is not None else None),
            "modules": modules, "returncode": code, "timed_out": timed_out,
            "seconds": round(time.monotonic() - start, 1),
            "failures": failures,
            "output_tail": "\n".join(output.rstrip().splitlines()[-40:])}


def report(scenarios_run: list[dict], modules: list[str],
           inconclusive: list[str], skipped: list[str] | None = None) -> dict:
    red = [run for run in scenarios_run
           if run["failures"] or run["returncode"] not in (0, None)]
    timeout = [run for run in scenarios_run if run["timed_out"]]
    if red:
        verdict = "red"
    elif timeout or inconclusive:
        verdict = "inconclusive"
    else:
        verdict = "green"
    return {"schema": "ameesh-horloge-passe/1",
            "generated_ts": _utcnow().isoformat(),
            "modules": modules,
            "scenarios": scenarios_run,
            "inconclusive": inconclusive,
            "skipped": skipped or [],
            "verdict": verdict}


def entry() -> int:
    parser = argparse.ArgumentParser(
        prog="test-horloge-passe.py",
        description="passe horloge/fuseau des tests, hors porte de fusion (#5427)")
    parser.add_argument("--scenario", action="append", choices=["23h45", "00h05", "fuseau"],
                        help="scénario à rejouer (défaut : les trois)")
    parser.add_argument("--module", action="append", default=[],
                        help="module à rejouer (remplace la sélection)")
    parser.add_argument("--all", action="store_true",
                        help="toute la suite (long : lancement manuel)")
    parser.add_argument("--json", metavar="RAPPORT", help="écrire le rapport JSON")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="borne d'un scénario en secondes (défaut %d)"
                             % DEFAULT_TIMEOUT)
    parser.add_argument("--self-check", action="store_true",
                        help="vérifier seulement libfaketime et les trois horloges")
    parser.add_argument("--list", action="store_true", help="afficher la sélection")
    parser.add_argument("--max-skew", type=int, default=MAX_SKEW_S,
                        help="écart maximal entre l'heure réelle et l'heure simulée, "
                             "en secondes (défaut %d ; 0 pour ne pas borner un "
                             "lancement manuel)" % MAX_SKEW_S)
    args = parser.parse_args()

    chosen = [sc for sc in scenarios()
              if not args.scenario or sc["name"] in args.scenario]
    if args.self_check:
        unavailable = []
        for scenario in chosen:
            try:
                env = _python_env(
                    _clock_env(dict(os.environ), fake_at=scenario["fake_at"],
                               tz=scenario["tz"]), REPO)
                if scenario["fake_at"] is not None:
                    check_clock(env, scenario["fake_at"])
                print("ok  %s (%s, TZ=%s)" % (scenario["name"], scenario["kind"],
                                              scenario["tz"]))
            except Unavailable as exc:
                unavailable.append("%s: %s" % (scenario["name"], exc))
                print("NON CONCLUANT %s : %s" % (scenario["name"], exc))
        return 77 if unavailable else 0

    if args.list:
        modules = args.module or (all_modules() if args.all else selection())
        print("\n".join(modules))
        return 0

    calendar = calendar_modules()
    seed = seed_modules()
    plans = []
    for scenario in chosen:
        modules = scenario_modules(scenario, override=args.module,
                                   everything=args.all, seed=seed,
                                   calendar=calendar)
        if modules:
            plans.append((scenario, modules))
    if not plans:
        print("aucun module à rejouer (liste horloge.txt vide, lint sans H3)")
        return 0

    if not os.environ.get("AMEESH_TEST_DSN") and not os.environ.get("AGENT_MESH_TEST_DSN"):
        print("note : AMEESH_TEST_DSN absent ; les modules qui demandent une base "
              "seront sautés ou en erreur", file=sys.stderr)

    runs, inconclusive, skipped, played = [], [], [], []
    for scenario, modules in plans:
        try:
            run = run_scenario(scenario, modules, timeout=args.timeout,
                               max_skew=args.max_skew)
        except NotDue as exc:
            skipped.append("%s: %s" % (scenario["name"], exc))
            print("PAS DUE %s : %s" % (scenario["name"], exc))
            continue
        except Unavailable as exc:
            inconclusive.append("%s: %s" % (scenario["name"], exc))
            print("NON CONCLUANT %s : %s" % (scenario["name"], exc))
            continue
        runs.append(run)
        played = _dedupe(played + modules)
        state = "ROUGE" if run["failures"] or run["returncode"] != 0 else "vert"
        print("%-8s %-8s %5.1f s  %d module(s)" % (
            run["name"], state, run["seconds"], len(modules)))
        for failure in run["failures"]:
            print("    " + failure)
        if run["timed_out"]:
            print("    NON CONCLUANT : délai de %d s dépassé" % args.timeout)

    payload = report(runs, played, inconclusive, skipped)
    if args.json:
        path = pathlib.Path(args.json)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        print("rapport : %s" % path)
    if payload["verdict"] == "red":
        print("\nUn rouge de cette passe est un test dépendant de l'environnement "
              "(canon §3) : défaut du test, à corriger à la source. Ouvrir un lot "
              "« test dépendant de l'environnement » avec le nom du test et le "
              "scénario ci-dessus.")
        return 1
    if payload["verdict"] == "inconclusive":
        print("\nPasse non concluante : rien n'est bloqué (canon §3), le scénario "
              "sera rejoué.")
        return 77
    print("\npasse verte : %d scénario(s), %d module(s), %d pas dû(s)"
          % (len(runs), len(played), len(skipped)))
    return 0


if __name__ == "__main__":
    sys.exit(entry())
