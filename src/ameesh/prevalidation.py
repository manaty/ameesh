# SPDX-License-Identifier: AGPL-3.0-only
"""Prévalidation au gel (#1770) : exécuter sur VM les tests associés au diff.

Règle préparée (#1616), activée par le bloc `prevalidation` du canon : le gel
d'un lot n'est valable que si la tête candidate a passé la sélection par
impact de la carte L161e et du plan L161f, exécutée sur une VM agents.

Ce module ne choisit pas les tests : il lit le plan de
`scripts/test-plan.py` (schéma `ameesh-test-plan/1`) et **refuse**, cause
nommée, tout ce que la CI élargirait — carte absente ou périmée, sélection
non sûre, repli sur la suite complète. La suite complète n'est jamais
exécutée par la prévalidation, et aucun repli local n'est permis :
l'exécution passe par `ameesh offload run --fallback none` sur un hôte
d'agents, jamais sur le poste de l'owner.

La preuve est lue dans la sortie de la VM : le flux `ameesh-test-event/1`
(événements `fin` par part, modules réellement exécutés = événements `test`)
et le marqueur de durée. Le rapport porte lot, SHA complet, base, commit de
carte, modules exécutés, hôte, résultat et durée ; un changement de tête
pendant la validation l'invalide. Ce rapport n'est pas un verdict : il ne
remplace aucune relecture (#1617, #1756).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Sequence

#: schéma du plan lu (`scripts/test-plan.py`, L161f)
PLAN_SCHEMA = "ameesh-test-plan/1"
#: schéma de la carte (`scripts/map.py`, L161e)
CARD_SCHEMA = "ameesh-carte/1"
#: schéma du flux de test (`scripts/test-parts.py`)
FLOW_SCHEMA = "ameesh-test-event/1"
#: schéma du rapport de prévalidation
REPORT_SCHEMA = "ameesh-prevalidation/1"

#: causes de refus d'une prévalidation ; le gel n'est pas écrit, la cause est dite
CAUSES = (
    "card-missing",       # carte absente, illisible ou sans commit
    "card-stale",         # carte trop vieille ou sans ancêtre commun avec la tête
    "selection-unsafe",   # le plan n'est pas sûr (fondation, bloc inconnu, retrait…)
    "selection-full",     # le plan retombe sur la suite complète : refus, jamais de repli
    "head-moved",         # le plan ne porte pas la tête candidate exacte
    "no-vm",              # VM absente, injoignable, job refusé ou arrêté (offload 75)
    "run-red",            # la sélection est rouge
    "run-incomplete",     # timeout, annulation, part sans fin : inexploitable (124/130/70)
    "transport-lost",     # connexion perdue, résultat non authentifié
    "block-missing",      # le canon ne porte pas le bloc `prevalidation` (#1770)
)


class PrevalidationError(Exception):
    """Un refus nommé : la prévalidation s'arrête, elle ne suppose rien.

    `cause` appartient à [`CAUSES`] ; ce refus n'écrit ni jalon ni verdict.
    """

    def __init__(self, cause: str, why: str = ""):
        super().__init__("%s: %s" % (cause, why) if why else cause)
        self.cause = cause
        self.why = why


def load_parameters(repo: str, commit: str, project: str):
    """Lit le canon relu (#1616) et rend `(document, bloc prevalidation)`.

    Le consommateur explicite refuse un bloc absent, cause `block-missing` :
    aucune sélection, aucun hôte et aucun délai ne se supposent. L'accroche
    automatique du gel ne l'appelle pas tant que la règle n'est pas adoptée.
    Le lecteur `mechanics` est importé ici pour rester nommément refusable.
    """
    try:
        from . import mechanics
    except ImportError as exc:  # lecteur absent : refus nommé, jamais de repli
        raise PrevalidationError("block-missing",
                                 "mechanics reader absent: %s" % exc) from exc
    document = mechanics.load(repo, commit, project)
    if document.prevalidation is None:
        raise PrevalidationError("block-missing",
                                 "%s has no prevalidation block" % document.source)
    return document, document.prevalidation
#: sort du run selon le code de sortie de `ameesh offload run --fallback none`
RUN_OUTCOMES = {0: "green", 1: "run-red", 75: "no-vm", 124: "run-incomplete",
                130: "run-incomplete", 70: "run-incomplete", 255: "transport-lost"}
#: marqueurs écrits par le script distant
SECONDS_MARK = "ameesh-prevalidation-seconds="
PART_MARK = "ameesh-prevalidation-part="
_SECONDS_RE = re.compile(r"^%s(\d+)\s*$" % re.escape(SECONDS_MARK), re.M)
_PART_RE = re.compile(r"^%s([A-Za-z0-9_.:-]+)\s*$" % re.escape(PART_MARK), re.M)


def _same_commit(a: str | None, b: str | None) -> bool:
    """Deux identifiants qui nomment le même commit (préfixe admis)."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


def _missing(problem: str) -> bool:
    text = (problem or "").lower()
    return ("absent" in text or "missing" in text or "introuvable" in text
            or "illisible" in text or "unreadable" in text)


@dataclass(frozen=True)
class Selection:
    """La sélection à exécuter, ou les causes de son refus (`ok` faux).

    `parts` ne porte que des listes de modules explicites : une part sans
    liste serait « le reste de la suite », que la prévalidation ne court
    jamais."""

    head: str
    base: str = ""
    card_commit: str = ""
    modules: tuple[str, ...] = ()
    parts: tuple[dict, ...] = ()
    causes: tuple[str, ...] = ()
    why: str = ""
    plan_path: str = ""

    @property
    def ok(self) -> bool:
        return not self.causes

    def proof(self, *, lot: int, host: str, run: dict, flow: dict | None = None,
              head_after: str = "") -> dict:
        """Le rapport de prévalidation, sans effet de verdict.

        `run` : `{"outcome", "exit", "seconds"}` ; `flow` : le flux lu
        (`modules`, `tests`, `fins`). `head_after` : la tête publiée relue
        après le run ; si elle diffère du SHA validé, le rapport est invalidé
        (`head-moved`) et ne vaut rien."""
        flow = flow or {}
        causes = list(self.causes)
        if head_after and not _same_commit(head_after, self.head):
            causes.append("head-moved")
        outcome = run.get("outcome") or ""
        if causes:
            outcome = "refused"
        elif outcome == "green":
            fins = list(flow.get("fins") or ())
            if not fins or any(f != "succes" for f in fins):
                outcome = "run-incomplete" if any(
                    f == "interrompu" for f in fins) or not fins else "run-red"
                causes.append(outcome)
        else:
            causes.append(outcome)
        unique: list[str] = []
        for cause in causes:
            if cause not in unique:
                unique.append(cause)
        return {
            "schema": REPORT_SCHEMA,
            "lot": int(lot), "sha": self.head, "base": self.base,
            "card": self.card_commit, "modules": sorted(flow.get("modules") or self.modules),
            "planned": list(self.modules),
            "parts": list(flow.get("parts") or ()),
            "tests": flow.get("tests"),
            "host": host, "outcome": outcome,
            "exit": run.get("exit"), "seconds": run.get("seconds"),
            "causes": unique, "ok": not unique,
        }


def read_plan(path: str) -> dict:
    """Le plan écrit par `scripts/test-plan.py plan --out` (jamais deviné)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError("plan illisible (%s): %s" % (path, exc)) from None
    if not isinstance(data, dict):
        raise ValueError("plan %s : objet attendu" % path)
    return data


def planned_modules(plan: dict) -> tuple[str, ...]:
    """Les modules que les parts du plan exécuteront, sans doublon.

    Le plan L161f (`scripts/test-plan.py`) porte les listes dans
    `parts.<pilote>[].modules` ; `selection.modules` n'est qu'un **compte**
    (`selected`, `impacted`, `contract` aussi). La sélection se lit donc dans
    ses parts : lire le compte comme une liste faisait échouer la commande sur
    un vrai plan (`TypeError`), défaut trouvé par la preuve réelle (#1770)."""
    modules: list[str] = []
    seen: set[str] = set()
    for _driver, listed in sorted((plan.get("parts") or {}).items()):
        for part in listed or ():
            for name in (part.get("modules") if isinstance(part, dict) else None) or ():
                text = str(name)
                if text not in seen:
                    seen.add(text)
                    modules.append(text)
    return tuple(modules)


def selection(plan: dict, *, head: str, base: str = "", plan_path: str = "") -> Selection:
    """La sélection d'un plan pour la tête `head`, ou les causes du refus.

    Un plan insuffisant est un **résultat**, pas une exception : le gel sera
    refusé avec la cause nommée."""
    causes: list[str] = []
    why: list[str] = []
    if not isinstance(plan, dict):
        return Selection(head=head, base=base, plan_path=plan_path,
                         causes=("selection-unsafe",), why="plan illisible")
    got = str(plan.get("head") or "")
    if not _same_commit(got, head):
        causes.append("head-moved")
        why.append("plan %s != tête %s" % (got[:12] or "—", head[:12] or "—"))
    card = plan.get("map") if isinstance(plan.get("map"), dict) else None
    card_commit = str((card or {}).get("commit") or "")
    if not card or not card.get("used"):
        problem = str((card or {}).get("problem") or "carte absente")
        causes.append("card-missing" if _missing(problem) else "card-stale")
        why.append(problem)
    sel = plan.get("selection") if isinstance(plan.get("selection"), dict) else {}
    modules = planned_modules(plan)
    mode = str(sel.get("mode") or "")
    if mode == "full":
        causes.append("selection-full")
        why.append(str(sel.get("why") or "repli sur la suite complète"))
    elif mode != "impacted":
        causes.append("selection-unsafe")
        why.append("mode de sélection %r" % (mode or "absent",))
    if not plan.get("sure"):
        causes.append("selection-unsafe")
        unsure = list(plan.get("unsure") or ())
        why.append("sélection non sûre: %s" % (unsure[0] if unsure else "raison absente"))
    if not plan.get("phase2_safe"):
        causes.append("selection-unsafe")
        why.append("phase 2 non sûre: %s" % (plan.get("phase2_why") or "raison absente"))
    if not modules:
        causes.append("selection-unsafe")
        why.append("aucun module sélectionné")
    contract = tuple(str(m) for m in (plan.get("contract") or ()))
    if contract and not set(contract) <= set(modules):
        causes.append("selection-unsafe")
        why.append("contrats absents de la sélection: %s"
                   % ", ".join(sorted(set(contract) - set(modules))[:3]))
    parts: list[dict] = []
    for driver, listed in sorted((plan.get("parts") or {}).items()):
        for part in listed or ():
            label = part.get("part") if isinstance(part, dict) else "?"
            listed_modules = part.get("modules") if isinstance(part, dict) else None
            if not isinstance(listed_modules, (list, tuple)) or not listed_modules:
                causes.append("selection-unsafe")
                why.append("part %s %s sans liste de modules" % (driver, label))
                continue
            parts.append({"driver": str(driver), "part": part.get("part"),
                          "of": part.get("of"), "label": part.get("label") or "",
                          "modules": tuple(str(m) for m in listed_modules),
                          "estimate_s": part.get("estimate_s")})
    announced = sel.get("selected")
    if isinstance(announced, int) and not isinstance(announced, bool) \
            and announced != len(modules):
        causes.append("selection-unsafe")
        why.append("plan incohérent: %d module(s) annoncés, %d dans les parts"
                   % (announced, len(modules)))
    unique: list[str] = []
    for cause in causes:
        if cause not in unique:
            unique.append(cause)
    return Selection(head=head, base=base, card_commit=card_commit,
                     modules=modules, parts=tuple(parts),
                     causes=tuple(unique), why=" ; ".join(why), plan_path=plan_path)


def host_allowed(host: str, *, local: str | None = None, deny: Sequence[str] = ()) -> tuple[bool, str]:
    """L'hôte est-il une VM d'agents (jamais le poste local, jamais un déni) ?

    `local` : nom de l'hôte courant ; `deny` : motifs d'hôtes interdits
    (configuration, `AMEESH_PREVALIDATION_DENY`). Une chaîne vide est
    refusée : sans hôte, l'offload retomberait localement."""
    name = (host or "").strip()
    if not name:
        return False, "no-vm"
    bare = name.rsplit("@", 1)[-1].split(":", 1)[0].strip().lower()
    if not bare:
        return False, "no-vm"
    here = (local if local is not None else (os.uname().nodename or "")).strip().lower()
    if here and (bare == here or here.startswith(bare + ".") or bare.startswith(here + ".")):
        return False, "no-vm"
    for pattern in deny:
        motif = (pattern or "").strip().lower()
        if motif and (motif == bare or motif in bare):
            return False, "no-vm"
    return True, ""


def remote_script(plan_path: str, parts: Sequence[dict],
                  python: str = "python3") -> str:
    """Le script exécuté sur la VM : chaque part, son flux, puis la durée.

    Chaque part est lancée séparément, sa sortie verbeuse est écartée (seul le
    flux JSONL revient, plus le marqueur de part) ; le code de sortie est
    conservé sans interrompre les autres parts. La carte (durées) n'est pas
    embarquée : les parts portent déjà leurs modules et `test-parts.py` se
    replie sur `tests/parts/durees.json`."""
    lignes = ["ec=0", "debut=$(date +%s)", "flux=$(mktemp -d)"]
    for index, part in enumerate(parts, 1):
        driver = _q(str(part.get("driver")))
        numero = _q(str(part.get("part")))
        fichier = '"$flux/part-%d.jsonl"' % index
        lignes.append('echo "%s%s:%s"' % (PART_MARK, str(part.get("driver")),
                                          part.get("part")))
        lignes.append("%s scripts/test-parts.py run --plan %s --part %s --driver %s"
                      " --flow %s >/dev/null 2>&1 || ec=$?"
                      % (python, _q(plan_path), numero, driver, fichier))
        lignes.append("cat %s 2>/dev/null || true" % fichier)
    lignes += ["fin=$(date +%s)", 'echo "%s$((fin-debut))"' % SECONDS_MARK,
               "rm -rf \"$flux\"", "exit $ec"]
    return "\n".join(lignes)


def _q(value: str) -> str:
    return shlex.quote(value)


def offload_argv(host: str, command: str, *, timeout: int | None = None,
                 executable: str = "ameesh") -> list[str]:
    """L'appel d'offload, toujours `--fallback none` : pas de repli local."""
    argv = [executable, "offload", "run", "--host", host, "--fallback", "none"]
    if timeout:
        argv += ["--timeout", str(int(timeout))]
    return argv + ["--", "bash", "-lc", command]


def classify(exit_code: int, output: str = "") -> str:
    """Le sort du run depuis le code de sortie de l'offload (contrat L159b).

    Le code 75 couvre l'absence d'hôte, le refus/arrêt du job **et** la
    connexion perdue : les notes de l'offload les distinguent."""
    code = int(exit_code)
    if code == 75 and "lost the connection" in (output or ""):
        return "transport-lost"
    return RUN_OUTCOMES.get(code, "run-incomplete")


def parse_flow(text: str) -> dict:
    """Le flux `ameesh-test-event/1` lu dans la sortie distante.

    Rend les modules réellement exécutés (événements `test`), le nombre de
    tests, les statuts de fin par part et les parts vues. Un part sans
    événement `fin` reste visible : c'est un run incomplet, jamais un vert."""
    modules: set = set()
    parts: list[str] = []
    fins: list[str] = []
    fins_par_part: dict = {}
    tests = 0
    courant = ""
    for ligne in (text or "").splitlines():
        ligne = ligne.strip()
        marque = _PART_RE.match(ligne)
        if marque:
            courant = marque.group(1)
            if courant not in parts:
                parts.append(courant)
            continue
        if not ligne.startswith("{"):
            continue
        try:
            event = json.loads(ligne)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("schema") != FLOW_SCHEMA:
            continue
        kind = event.get("type")
        part = courant or str(event.get("part") or "")
        if kind == "test":
            tests += 1
            if event.get("module"):
                modules.add(str(event["module"]))
        elif kind == "fin":
            statut = str(event.get("statut") or "")
            fins.append(statut)
            if part:
                fins_par_part[part] = statut
                if part not in parts:
                    parts.append(part)
    return {"modules": tuple(sorted(modules)), "tests": tests, "parts": tuple(parts),
            "fins": tuple(fins), "fins_par_part": fins_par_part}


def parse_seconds(text: str) -> float | None:
    """La durée distante écrite par le script, en secondes (None si absente)."""
    found = _SECONDS_RE.search(text or "")
    return float(found.group(1)) if found else None


def run_offload(host: str, command: str, *, timeout: int | None = None,
                cwd: str | None = None, env: dict | None = None,
                runner=subprocess.run, executable: str = "ameesh") -> dict:
    """Exécute la sélection sur la VM et rend `{outcome, exit, seconds, host, flow}`.

    Aucun repli : un transport perdu n'est jamais rejoué ici (la suite peut
    tourner encore sur la VM). La durée distante est lue dans la sortie, la
    durée murale sert de repli."""
    argv = offload_argv(host, command, timeout=timeout, executable=executable)
    started = time.time()
    proc = runner(argv, cwd=cwd, env=env, capture_output=True, text=True)
    output = (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
    result = {"outcome": classify(proc.returncode, output), "exit": int(proc.returncode),
              "host": host, "flow": parse_flow(output)}
    result["seconds"] = parse_seconds(output)
    if result["seconds"] is None:
        result["seconds"] = round(time.time() - started, 1)
    return result


# --------------------------------------------------------------------------
# la sélection doit rester dans ce que le canon déclare (#1770)
# --------------------------------------------------------------------------

def meets_canon(plan: dict, params) -> tuple[bool, str]:
    """La sélection du plan tient-elle exactement dans le canon ?

    Le canon déclare les classes retenues ; le plan porte la classe de chaque
    module (`classes`) et la liste des contrats. La sélection doit être
    exactement `classes déclarées ∪ contrats` : un module en trop serait un
    élargissement, un module manquant un contrat perdu. Sans classes lisibles,
    rien ne se prouve : refus."""
    classes = plan.get("classes") if isinstance(plan, dict) else None
    if not isinstance(classes, dict) or not classes:
        return False, "plan sans classes : la sélection ne se prouve pas"
    declared = tuple(getattr(params, "classes", ()) or ())
    if not declared:
        return False, "canon sans classes déclarées"
    contract = {str(m) for m in (plan.get("contract") or ())}
    expected = {str(m) for m, entry in classes.items()
                if isinstance(entry, (list, tuple)) and entry
                and str(entry[0]) in declared}
    expected |= contract
    chosen = set(planned_modules(plan))
    missing = sorted(expected - chosen)
    extra = sorted(chosen - expected)
    if missing:
        return False, "modules attendus absents de la sélection: %s" % ", ".join(missing[:3])
    if extra:
        return False, "modules hors des classes déclarées: %s" % ", ".join(extra[:3])
    return True, ""


def published_head(ref: str, *, repo: str = ".", remote: str = "origin",
                   runner=subprocess.run) -> str:
    """La tête d'une référence **publiée** : ce que porte le distant.

    Un ref local ne prouve pas la publication ; une lecture impossible refuse
    (`head-moved`) plutôt que de valider un résultat sur une tête incertaine."""
    proc = runner(["git", "-C", repo, "ls-remote", remote, ref],
                  capture_output=True, text=True)
    if getattr(proc, "returncode", 1) != 0:
        raise PrevalidationError("head-moved", "tête publiée illisible: %s" % ref)
    for ligne in (getattr(proc, "stdout", "") or "").splitlines():
        champs = ligne.split()
        if len(champs) == 2 and champs[1] in (ref, "refs/heads/" + ref):
            return champs[0]
    raise PrevalidationError("head-moved", "référence publiée introuvable: %s" % ref)


def validate(*, lot: int, plan_path: str, head: str, host: str, params,
             base: str = "", repo: str = "", published_ref: str = "",
             remote: str = "origin", local: str | None = None,
             cwd: str | None = None, env: dict | None = None,
             runner=subprocess.run, git_runner=subprocess.run,
             executable: str = "ameesh") -> dict:
    """Le geste complet : contrôles du canon, exécution distante, rapport.

    Refuse avant tout run un hôte hors du canon, un contrat déclaré introuvable
    ou une sélection élargie ; aucun repli local, aucune écriture de jalon. Le
    rapport rendu n'est pas un verdict (#1617, #1756)."""
    if not isinstance(lot, int) or lot <= 0:
        raise PrevalidationError("selection-unsafe", "lot invalide: %r" % (lot,))
    name = (host or "").strip()
    if name not in tuple(getattr(params, "hosts", ()) or ()):
        raise PrevalidationError("no-vm", "hôte %r hors des hôtes déclarés au canon" % name)
    allowed, why = host_allowed(name, local=local)
    if not allowed:
        raise PrevalidationError("no-vm", why)
    for path in getattr(params, "contracts", ()) or ():
        full = os.path.join(repo, str(path)) if repo else str(path)
        if not os.path.isfile(full):
            raise PrevalidationError("selection-unsafe",
                                     "contrat déclaré introuvable: %s" % path)
    plan = read_plan(plan_path)
    within, why = meets_canon(plan, params)
    chosen = selection(plan, head=head, base=base, plan_path=plan_path)
    if not chosen.ok:
        raise PrevalidationError(chosen.causes[0], chosen.why)
    if not within:
        raise PrevalidationError("selection-unsafe", why)
    result = run_offload(name, remote_script(plan_path, chosen.parts),
                         timeout=int(getattr(params, "timeout_seconds", 0) or 0),
                         cwd=cwd, env=env, runner=runner, executable=executable)
    after = ""
    if published_ref:
        after = published_head(published_ref, repo=repo or ".", remote=remote,
                               runner=git_runner)
    return chosen.proof(lot=lot, host=name, run=result, flow=result.get("flow"),
                        head_after=after)


def record(conn, report: dict) -> None:
    """Persiste le rapport APRÈS le run distant (0450), hors de toute transaction.

    Rien n'est écrit avant l'exécution : aucun verrou de base n'est tenu pendant
    que la VM exécute la sélection. Le rapport n'est pas un verdict (#1617,
    #1756) ; cette écriture n'active aucune règle."""
    from . import storage
    storage.of(conn).prevalidation.record(report)


# --------------------------------------------------------------------------
# la commande explicite : refus nommé, aucun jalon, accroche automatique inactive
# --------------------------------------------------------------------------

#: sous-commandes `ameesh work …` portées par ce module
COMMANDS = ("prevalidate",)


def add_parsers(work_sub, func) -> None:
    """`ameesh work prevalidate` : prévalidation explicite d'une tête candidate."""
    parser = work_sub.add_parser(
        "prevalidate", help="#1770 : exécuter la sélection du canon sur une VM avant un gel")
    parser.add_argument("lot", type=int)
    parser.add_argument("--sha", required=True, help="tête candidate exacte")
    parser.add_argument("--plan", required=True, help="plan L161f (scripts/test-plan.py)")
    parser.add_argument("--host", required=True, help="VM agents (jamais le poste local)")
    parser.add_argument("--base", default="")
    parser.add_argument("--repo", default="", help="dépôt du lot (contrats, tête publiée)")
    parser.add_argument("--canon-repo", default="")
    parser.add_argument("--canon-commit", default="", help="commit exact du canon relu")
    parser.add_argument("--project", default="ameesh")
    parser.add_argument("--published-ref", default="",
                        help="référence relue au distant après le run (invalide si elle bouge)")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=func)


def run(cfg, db, args) -> int:
    """La commande explicite refuse un canon sans bloc, cause `block-missing`.

    Elle n'écrit ni jalon ni verdict : l'accroche automatique du gel reste
    inactive tant que la règle n'est pas adoptée."""
    from . import mechanics  # refus nommé si le lecteur manque (load_parameters)
    try:
        params = None
        if args.canon_commit:
            _, params = load_parameters(args.canon_repo or args.repo or ".",
                                        args.canon_commit, args.project)
        if params is None:
            raise PrevalidationError("block-missing",
                                     "aucun canon relu fourni (--canon-commit)")
        report = validate(lot=args.lot, plan_path=args.plan, head=args.sha,
                          host=args.host, params=params, base=args.base,
                          repo=args.repo or "", published_ref=args.published_ref,
                          remote=args.remote)
    except PrevalidationError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"schema": REPORT_SCHEMA, "lot": args.lot,
                              "outcome": "refused", "ok": False,
                              "causes": [exc.cause], "why": exc.why}, sort_keys=True))
        else:
            print("prévalidation refusée : %s%s"
                  % (exc.cause, (" — " + exc.why) if exc.why else ""))
        return 1
    except (ValueError, mechanics.MechanicsError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"schema": REPORT_SCHEMA, "lot": args.lot,
                              "outcome": "refused", "ok": False,
                              "causes": ["selection-unsafe"], "why": str(exc)},
                             sort_keys=True))
        else:
            print("prévalidation refusée : %s" % exc)
        return 1
    record(db, report)  # le rapport s'écrit après le run, hors transaction
    if getattr(args, "json", False):
        print(json.dumps(report, sort_keys=True))
    elif report["ok"]:
        print("prévalidation verte : lot %s, %s, %s module(s), %s s sur %s"
              % (report["lot"], report["sha"][:12], len(report["modules"]),
                 report["seconds"], report["host"]))
    else:
        print("prévalidation refusée : %s" % ", ".join(report["causes"]))
    return 0 if report["ok"] else 1
