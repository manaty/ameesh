#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Paquet horaire déclenché par minuterie, sans agent (#3845, #1606 point 1).

    scripts/paquet-horaire.py tick --repo ~/ameesh-composeur --target main

`scripts/paquet-auto.py` (#3807) compose sans jugement, mais un agent devait
encore le lancer chaque heure, créer le lot porteur, poser « verdict non
requis », demander la ci-full, puis, sur un rouge, trouver l'unité fautive et
recomposer (h-32 : bisect à la main pour #3779). Ce script fait ces gestes, un
**tic** à la fois, sans état local : tout ce qu'il sait vient de la base
(lots, journal du porteur) et de la forge, si bien qu'un tic perdu, un hôte
arrêté ou un bail repris par un autre candidat ne laisse rien en suspens.

Un tic (la minuterie `deploy/systemd/ameesh-paquet-horaire.timer`, sous
`ameesh role run`, ne le lance que sur l'hôte qui détient le bail) :

1. **Réservation (§12).** Un porteur de paquet ouvert sur la cible (branche
   `integration/paquet-*` ou titre « Paquet horaire h-… ») la réserve : le tic
   suit ce porteur s'il est à lui (étapes 3-4), sinon ne fait rien.
2. **Composition, une fois par heure.** Cible libre et aucun porteur horaire
   créé dans l'heure (Europe/Paris) : la liste calculée est lue
   (`paquet-candidats.py` depuis un worktree à la tête exacte de la cible) ;
   sans lot prêt, rien n'est créé. Sinon le lot porteur « Paquet horaire
   h-AAAAMMJJ-NN » est créé et `paquet-auto.py` compose, pousse et gèle.
3. **Publication.** Sur la tête gelée sans demande `ci-full` : « verdict non
   requis » **seulement** si aucune unité n'est une résolution (fusions
   propres, §2 : aucun avis) — avec une résolution le geste reste au pilote
   (#1979) et le courrier le dit ; puis `ameesh ci run <porteur> --full`, qui
   arme la fusion automatique (`packet_automerge`, tic de release).
4. **Rouge.** Sur la ci-full rouge de la tête gelée : un rouge **déterministe**
   (chaque échec de test propre au paquet attribué par #3801 à une seule
   unité probable, rejoué rouge en local sur la tête) retire ces unités ; le
   paquet est recomposé sans elles (`paquet-auto.py --force --exclude`), **une
   fois**, et l'auteur de chaque unité retirée reçoit un courrier. Un rouge
   non concluant (§3 : run annulé ou hors délai, échec de job sans test,
   échec non attribuable ou ambigu, test vert en local) ne retire rien : il
   est consigné au journal du porteur et le pilote est prévenu, une fois.
5. Un courrier au pilote à chaque étape (tête, unités, écartés, CI).

Ce script ne fusionne jamais : la fusion reste celle de `packet_automerge`
sur la tête gelée, CI requise verte. Les marques d'idempotence sont des notes
`paquet-horaire: …` au journal du porteur.

Codes de retour : 0 tic fait (y compris « rien à faire »), 1 échec
opérationnel, 2 entrée invalide.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")
TITLE_PREFIX = "Paquet horaire "
HOURLY_RE = re.compile(r"^Paquet horaire (h-(\d{8})-(\d+))\b")
CARRIER_BRANCH_PREFIX = "integration/paquet"
OPEN_STATES = ("intake", "build", "qa", "blocked", "waiting_human")
MARK = "paquet-horaire: "
MARK_CI = MARK + "ci demandée "
MARK_RECOMPOSED = MARK + "recomposition "
MARK_RED = MARK + "rouge consigné "
MARK_WAIT = MARK + "verdict au pilote "
MAX_RECOMPOSITIONS = 1
CREATED_RE = re.compile(r"^lot #(\d+) created", re.MULTILINE)
SCRIPT_DIR = Path(__file__).resolve().parent
LIST_LIMIT = 300
COMMAND_TIMEOUT = 600
COMPOSE_TIMEOUT = 3 * 3600
REPLAY_TIMEOUT = 900


class HoraireError(RuntimeError):
    """Un geste a échoué ; le message porte l'état atteint."""


class InputError(HoraireError):
    """Un argument invalide."""


def _load_auto():
    """`paquet-auto.py` en module : mêmes lecteurs de manifeste et de tests."""
    spec = importlib.util.spec_from_file_location(
        "paquet_auto", SCRIPT_DIR / "paquet-auto.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("paquet_auto", module)
    spec.loader.exec_module(module)
    return module


auto = _load_auto()


# ---------------------------------------------------------------------------
# Commandes (injectables pour les tests)
# ---------------------------------------------------------------------------


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def text(self) -> str:
        return (self.stdout + self.stderr).strip()


class Shell:
    """Lance les commandes ; les tests la remplacent par un faux."""

    def run(self, cmd: list[str], *, cwd: Path | None = None,
            input_text: str | None = None,
            timeout: int = COMMAND_TIMEOUT) -> Result:
        try:
            proc = subprocess.run(
                cmd, cwd=str(cwd) if cwd else None,
                input=input_text, text=True, capture_output=True,
                stdin=None if input_text is not None else subprocess.DEVNULL,
                timeout=timeout)
        except FileNotFoundError:
            return Result(127, "", "commande introuvable : %s" % cmd[0])
        except subprocess.TimeoutExpired:
            return Result(124, "", "délai de %s s dépassé : %s" % (timeout, cmd[0]))
        return Result(proc.returncode, proc.stdout or "", proc.stderr or "")


@dataclass
class Context:
    repo: Path
    target: str
    project: str
    pilot: str
    actor: str
    shell: Shell
    python: str = sys.executable
    dry_run: bool = False
    now: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)

    def say(self, text: str) -> None:
        self.log.append(text)
        print(text, flush=True)

    def ameesh(self, *args: str, input_text: str | None = None,
               timeout: int = COMMAND_TIMEOUT) -> Result:
        return self.shell.run(["ameesh", *args], cwd=self.repo,
                              input_text=input_text, timeout=timeout)

    def must(self, *args: str, what: str, timeout: int = COMMAND_TIMEOUT) -> Result:
        result = self.ameesh(*args, timeout=timeout)
        if result.returncode != 0:
            raise HoraireError("%s : rc %d : %s"
                               % (what, result.returncode, auto._tail(result.text, 20)))
        return result

    def git(self, *args: str, timeout: int = COMMAND_TIMEOUT) -> Result:
        return self.shell.run(["git", "-C", str(self.repo), *args], timeout=timeout)

    def note(self, lot: int, text: str) -> None:
        if self.dry_run:
            self.say("[essai] note #%d : %s" % (lot, text))
            return
        self.must("work", "note", str(lot), text, "--actor", self.actor,
                  what="note au porteur #%d" % lot)

    def mail(self, to: str, lot: int, body: str) -> None:
        if self.dry_run:
            self.say("[essai] courrier à %s (#%d) : %s" % (to, lot, body.splitlines()[0]))
            return
        result = self.shell.run(["agent-mail", "send", to, "-", "--lot", str(lot)],
                                input_text=body, timeout=120)
        if result.returncode != 0:
            # Un courrier perdu ne défait pas le geste : il est dit au journal.
            self.say("courrier à %s non envoyé : %s" % (to, auto._tail(result.text, 5)))


# ---------------------------------------------------------------------------
# Lectures
# ---------------------------------------------------------------------------


def _json(result: Result, what: str):
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise HoraireError("%s : JSON illisible : %s" % (what, auto._tail(result.text, 5))) from exc


def open_lots(ctx: Context) -> list[dict]:
    rows: dict[int, dict] = {}
    for state in OPEN_STATES:
        result = ctx.must("work", "list", "--json", "--state", state,
                          "--limit", str(LIST_LIMIT), what="lots %s" % state)
        for row in _json(result, "work list") or []:
            rows[int(row["id"])] = row
    return list(rows.values())


def recent_lots(ctx: Context) -> list[dict]:
    result = ctx.must("work", "list", "--json", "--limit", str(LIST_LIMIT),
                      what="lots récents")
    return _json(result, "work list") or []


def is_carrier(row: dict, project: str, target: str) -> bool:
    if (row.get("app") or "") not in ("", project):
        return False
    branch = str(row.get("branch") or "")
    title = str(row.get("title") or "")
    if not branch.startswith(CARRIER_BRANCH_PREFIX) and not HOURLY_RE.match(title):
        return False
    aimed = str(row.get("branch_target") or "")
    return aimed in ("", target)


def is_hourly(row: dict) -> bool:
    return bool(HOURLY_RE.match(str(row.get("title") or "")))


def hour_start(now: float) -> float:
    local = datetime.fromtimestamp(now, TZ)
    return local.replace(minute=0, second=0, microsecond=0).timestamp()


def slot_served(rows: list[dict], now: float, project: str, target: str) -> bool:
    """Un porteur horaire a-t-il été créé dans l'heure courante ?"""
    start = hour_start(now)
    return any(is_hourly(row) and is_carrier(row, project, target)
               and float(row.get("created_ts") or 0) >= start for row in rows)


def next_name(ctx: Context) -> str:
    """h-AAAAMMJJ-NN, NN = suivant le plus grand du jour (branches d'origin)."""
    day = datetime.fromtimestamp(ctx.now, TZ).strftime("%Y%m%d")
    pattern = "refs/heads/%sh-%s-*" % (auto.PACKAGE_PREFIX, day)
    result = ctx.git("ls-remote", "origin", pattern, timeout=120)
    if result.returncode != 0:
        raise HoraireError("ls-remote : %s" % auto._tail(result.text, 5))
    numbers = [int(found) for found in re.findall(
        r"paquet-h-%s-(\d+)\s*$" % day, result.stdout, re.MULTILINE)]
    return "h-%s-%02d" % (day, (max(numbers) + 1) if numbers else 1)


def show(ctx: Context, lot: int) -> dict:
    return _json(ctx.must("work", "show", str(lot), "--json",
                          what="lot #%d" % lot), "work show")


def marks(item: dict) -> list[str]:
    return [str(ev.get("note") or "") for ev in item.get("events") or []
            if str(ev.get("note") or "").startswith(MARK)]


def frozen_sha(item: dict) -> str:
    freeze = item.get("freeze") or {}
    declared = freeze.get("declared") or {}
    return str(declared.get("sha") or "").strip().lower()


def ci_status(ctx: Context, lot: int) -> dict:
    return _json(ctx.must("ci", "status", str(lot), "--json",
                          what="ci status #%d" % lot), "ci status")


def run_for(report: dict, sha: str) -> dict | None:
    """Le run le plus récent de la tête gelée (`ci status` liste du plus récent)."""
    for run in report.get("runs") or []:
        if str(run.get("head_sha") or "").lower() == sha:
            return run
    return None


# ---------------------------------------------------------------------------
# Composition et unités
# ---------------------------------------------------------------------------


@dataclass
class UnitRow:
    lots: list[int]
    source: str
    note: str

    @property
    def resolution(self) -> bool:
        return len(self.lots) > 1 or "résolution" in self.note.lower() \
            or "resolution" in self.note.lower()


def composition_units(text: str) -> list[UnitRow]:
    """Les unités de la table `## Composition` (gabarit #3629), avec leur note."""
    rows = auto.parse_composition_table(text)
    notes: list[str] = []
    in_table = False
    for line in text.splitlines():
        if line.strip().startswith("## Composition"):
            in_table = True
            continue
        if in_table and line.strip().startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and re.search(r"#\d+", cells[1]):
                notes.append(cells[-1] if len(cells) >= 5 else "")
        elif in_table and notes:
            break
    notes += [""] * (len(rows) - len(notes))
    return [UnitRow(lots, source, note) for (lots, source), note in zip(rows, notes)]


def manifest_at(ctx: Context, item: dict, sha: str) -> str:
    branch = str(item.get("branch") or "")
    name = branch[len(auto.PACKAGE_PREFIX):] if branch.startswith(auto.PACKAGE_PREFIX) else ""
    if not name:
        raise HoraireError("porteur #%s sans branche %s*" % (item.get("id"), auto.PACKAGE_PREFIX))
    ctx.git("fetch", "--no-tags", "origin", branch, timeout=300)
    result = ctx.git("show", "%s:%s" % (sha, auto.manifest_rel(name)))
    if result.returncode != 0:
        raise HoraireError("manifeste de %s illisible sur %s : %s"
                           % (branch, sha[:12], auto._tail(result.text, 5)))
    return result.stdout


def paquet_auto(ctx: Context, name: str, carrier: int, *,
                exclude: list[int] = (), force: bool = False) -> Result:
    cmd = [ctx.python, str(SCRIPT_DIR / "paquet-auto.py"), "--repo", str(ctx.repo),
           "--target", ctx.target, "--name", name, "--carrier", str(carrier),
           "--pilot", ctx.pilot]
    if force:
        cmd.append("--force")
    for lot in exclude:
        cmd += ["--exclude", str(lot)]
    if ctx.dry_run:
        ctx.say("[essai] %s" % " ".join(cmd))
        return Result(0, "aucun lot prêt : rien à composer (essai)\n", "")
    return ctx.shell.run(cmd, cwd=ctx.repo, timeout=COMPOSE_TIMEOUT)


def ready_lots(ctx: Context) -> list[int]:
    """La liste calculée, lue depuis un worktree à la tête exacte de la cible."""
    fetched = ctx.git("fetch", "--no-tags", "origin", ctx.target, timeout=300)
    if fetched.returncode != 0:
        raise HoraireError("fetch %s : %s" % (ctx.target, auto._tail(fetched.text, 5)))
    head = ctx.git("rev-parse", "origin/%s^{commit}" % ctx.target)
    if head.returncode != 0:
        raise HoraireError("tête de %s illisible" % ctx.target)
    base = head.stdout.strip()
    parent = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    path = Path(tempfile.mkdtemp(prefix="paquet-horaire-", dir=str(parent)))
    path.rmdir()
    added = ctx.git("worktree", "add", "--detach", str(path), base)
    if added.returncode != 0:
        raise HoraireError("worktree : %s" % auto._tail(added.text, 5))
    try:
        result = ctx.shell.run(
            [ctx.python, str(path / auto.CANDIDATES_SCRIPT), "--repo", str(path),
             "--target", ctx.target, "--base", base, "--json"],
            cwd=path, timeout=COMMAND_TIMEOUT)
        if result.returncode not in (0, 3):
            raise HoraireError("paquet-candidats rc %d : %s"
                               % (result.returncode, auto._tail(result.text, 10)))
        payload = _json(result, "paquet-candidats")
        errored = {int(item.get("work_item") or item.get("lot") or 0)
                   for item in payload.get("errors_by_lot") or []}
        return [int(lot) for lot in payload.get("ready") or [] if int(lot) not in errored]
    finally:
        ctx.git("worktree", "remove", "--force", str(path))
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Rouge : déterministe ou non concluant
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    deterministic: bool
    lots: list[int]
    tests: list[str]
    why: str


def classify(run: dict) -> Verdict:
    """Le rouge est-il attribué sans ambiguïté par #3801 ?

    Déterministe seulement si le run a échoué (`failure`), porte au moins un
    échec de test propre au paquet, et si **chaque** échec propre est attribué
    (`probable`) à une **seule** meilleure unité. Un échec de job sans test, un
    test non attribuable ou partagé entre unités, un run annulé : non concluant.
    """
    status = str(run.get("status") or "")
    if status != "failure":
        return Verdict(False, [], [], "run %s, pas un échec de test" % (status or "?"))
    own = [f for f in run.get("failures") or [] if not f.get("inherited")]
    if not own:
        return Verdict(False, [], [], "aucun échec propre au paquet (hérités seulement)")
    if any(str(f.get("part") or "") == "job" or not f.get("test") for f in own):
        return Verdict(False, [], [], "échec de job sans test (délai, démarrage)")
    attribution = run.get("attribution") or {}
    if attribution.get("status") != "ok":
        return Verdict(False, [], [], "attribution #3801 : %s"
                       % (attribution.get("status") or "absente"))
    by_test = {str(t.get("test") or ""): t for t in attribution.get("tests") or []}
    lots: list[int] = []
    tests: list[str] = []
    for failure in own:
        name = str(failure.get("test"))
        entry = by_test.get(name)
        if not entry or entry.get("status") != "probable":
            return Verdict(False, [], [], "%s non attribuable" % name)
        best = [unit for unit in entry.get("units") or [] if unit.get("best")]
        if len(best) != 1:
            return Verdict(False, [], [], "%s : %d unités à égalité" % (name, len(best)))
        lot = int(best[0]["lot"])
        tests.append(name)
        if lot not in lots:
            lots.append(lot)
    return Verdict(True, lots, tests, "attribué par #3801")


def replay_locally(ctx: Context, sha: str, tests: list[str]) -> str:
    """Rejoue les tests rouges sur la tête gelée ; "" si rouge confirmé."""
    if ctx.dry_run:
        return ""
    parent = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    path = Path(tempfile.mkdtemp(prefix="paquet-horaire-rejeu-", dir=str(parent)))
    path.rmdir()
    added = ctx.git("worktree", "add", "--detach", str(path), sha)
    if added.returncode != 0:
        return "rejeu local impossible (worktree : %s)" % auto._tail(added.text, 3)
    try:
        run = auto.run_tests(path, "rejeu", sorted(set(tests)), timeout=REPLAY_TIMEOUT)
        if run.green:
            return "vert en local (%d test(s)) : rouge non reproduit" % run.tests
        if run.inconclusive:
            return "rejeu local non concluant (%s)" % run.motive()
        return ""
    finally:
        ctx.git("worktree", "remove", "--force", str(path))
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Étapes
# ---------------------------------------------------------------------------


def compose(ctx: Context, rows: list[dict]) -> str:
    if slot_served(rows, ctx.now, ctx.project, ctx.target):
        return "porteur horaire déjà créé dans l'heure : rien à faire"
    ready = ready_lots(ctx)
    if not ready:
        return "aucun lot prêt sur %s : rien à faire" % ctx.target
    name = next_name(ctx)
    branch = auto.package_branch(name)
    title = "%s%s — composition automatique (§11 bis, §12)" % (TITLE_PREFIX, name)
    if ctx.dry_run:
        ctx.say("[essai] porteur « %s », %d lot(s) prêt(s) : %s"
                % (title, len(ready), ", ".join("#%d" % lot for lot in ready)))
        return "essai : composition de %s" % name
    created = ctx.must(
        "work", "add", "--title", title, "--type", "evolution",
        "--app", ctx.project, "--assignee", ctx.pilot, "--priority", "1",
        "--branch", branch, "--target", ctx.target,
        "--source", "paquet-horaire (#3845), minuterie",
        "--body", "Paquet horaire composé sans agent par scripts/paquet-horaire.py "
                  "(#3845) : %d lot(s) prêt(s) à la lecture (%s)."
                  % (len(ready), ", ".join("#%d" % lot for lot in ready)),
        "--actor", ctx.actor, what="création du porteur")
    found = CREATED_RE.search(created.stdout)
    if not found:
        raise HoraireError("porteur créé sans identifiant lisible : %s"
                           % auto._tail(created.text, 5))
    carrier = int(found.group(1))
    ctx.say("porteur #%d : %s" % (carrier, title))
    result = paquet_auto(ctx, name, carrier)
    return after_compose(ctx, carrier, name, result, step="composition")


def after_compose(ctx: Context, carrier: int, name: str, result: Result, *,
                  step: str) -> str:
    if result.returncode != 0:
        ctx.note(carrier, MARK + "%s en échec (rc %d)" % (step, result.returncode))
        ctx.mail(ctx.pilot, carrier,
                 "#%d %s : paquet-auto.py a échoué (rc %d) ; un agent lit.\n\n%s"
                 % (carrier, name, result.returncode, auto._tail(result.text, 30)))
        raise HoraireError("paquet-auto rc %d pour %s" % (result.returncode, name))
    if re.search(r"^(aucun lot prêt|aucune unité)", result.stdout, re.MULTILINE):
        reason = "rien composé (%s)" % step
        if not ctx.dry_run:
            ctx.must("work", "close", str(carrier), "--abandoned",
                     "--note", MARK + reason, "--actor", ctx.actor,
                     what="clôture du porteur vide #%d" % carrier)
        ctx.mail(ctx.pilot, carrier, "#%d %s : %s, porteur clos.\n\n%s"
                 % (carrier, name, reason, auto._tail(result.stdout, 15)))
        return "%s : %s" % (name, reason)
    item = show(ctx, carrier)
    return publish(ctx, item)


def publish(ctx: Context, item: dict) -> str:
    """Tête gelée : « verdict non requis » si aucune résolution, puis ci-full."""
    carrier = int(item["id"])
    sha = frozen_sha(item)
    if not sha:
        return "#%d sans tête gelée : rien à publier" % carrier
    units = composition_units(manifest_at(ctx, item, sha))
    resolutions = [unit for unit in units if unit.resolution]
    lines = ["#%d %s : tête gelée %s, %d unité(s)."
             % (carrier, item.get("branch"), sha[:12], len(units))]
    lines += ["- %s %s%s" % (", ".join("#%d" % lot for lot in unit.lots), unit.source[:12],
                             " (résolution)" if unit.resolution else "")
              for unit in units]
    if resolutions:
        lines.append("Résolution(s) présente(s) : « verdict non requis » reste au "
                     "pilote (#1979) ; la fusion automatique attend ce geste.")
        if MARK_WAIT + sha not in marks(item):
            ctx.note(carrier, MARK_WAIT + sha)
    elif not ctx.dry_run:
        ctx.must("work", "reviewer", str(carrier), "--no-verdict", "--actor", ctx.actor,
                 what="verdict non requis #%d" % carrier)
        lines.append("Fusions propres seulement : « verdict non requis » posé (§2).")
    if not ctx.dry_run:
        ctx.must("ci", "run", str(carrier), "--full", "--sha", sha, "--avis", "0",
                 "--reason", "paquet horaire automatique (#3845)",
                 what="ci-full #%d" % carrier)
    ctx.note(carrier, MARK_CI + sha)
    lines.append("ci-full demandée sur %s ; fusion automatique à la CI verte." % sha[:12])
    ctx.mail(ctx.pilot, carrier, "\n".join(lines))
    return "#%d : ci-full demandée sur %s" % (carrier, sha[:12])


def follow(ctx: Context, item: dict) -> str:
    """Un porteur horaire ouvert : publier, attendre, ou traiter un rouge."""
    carrier = int(item["id"])
    sha = frozen_sha(item)
    if not sha:
        return "#%d sans gel : composition en cours ou en échec, rien à faire" % carrier
    noted = marks(item)
    if MARK_CI + sha not in noted:
        return publish(ctx, item)
    run = run_for(ci_status(ctx, carrier), sha)
    if run is None or run.get("status") in ("running", "queued", "pending", None):
        return "#%d : ci-full en cours sur %s" % (carrier, sha[:12])
    if run.get("status") == "success":
        return "#%d : ci-full verte sur %s, fusion automatique" % (carrier, sha[:12])
    run_id = str(run.get("run_id") or "?")
    if any(note.startswith(MARK_RED + run_id) for note in noted):
        return "#%d : rouge %s déjà consigné" % (carrier, run_id)
    verdict = classify(run)
    if verdict.deterministic:
        why = replay_locally(ctx, sha, verdict.tests)
        if why:
            verdict = Verdict(False, verdict.lots, verdict.tests, why)
    recomposed = sum(1 for note in noted if note.startswith(MARK_RECOMPOSED))
    if verdict.deterministic and recomposed < MAX_RECOMPOSITIONS:
        return recompose(ctx, item, run, verdict)
    reason = verdict.why if not verdict.deterministic else \
        "retrait automatique épuisé (%d recomposition)" % recomposed
    ctx.note(carrier, MARK_RED + "%s : %s" % (run_id, reason))
    ctx.mail(ctx.pilot, carrier,
             "#%d %s : ci-full rouge (run %s, %s) ; rien retiré : %s. "
             "Un agent lit (ameesh ci status %d)."
             % (carrier, item.get("branch"), run_id, sha[:12], reason, carrier))
    return "#%d : rouge consigné (%s)" % (carrier, reason)


def recompose(ctx: Context, item: dict, run: dict, verdict: Verdict) -> str:
    carrier = int(item["id"])
    branch = str(item.get("branch") or "")
    name = branch[len(auto.PACKAGE_PREFIX):]
    run_id = str(run.get("run_id") or "?")
    removed = ", ".join("#%d" % lot for lot in verdict.lots)
    ctx.note(carrier, MARK_RED + "%s : déterministe, retrait de %s" % (run_id, removed))
    ctx.note(carrier, MARK_RECOMPOSED + "1 sans %s (run %s)" % (removed, run_id))
    for lot in verdict.lots:
        try:
            author = str(show(ctx, lot).get("assignee") or "")
        except HoraireError:
            author = ""
        if author:
            ctx.mail(author, lot,
                     "#%d retiré du paquet %s : la ci-full %s (tête %s) est rouge, "
                     "attribuée à ton unité par la carte d'impact (#3801) et "
                     "rouge en local. Tests : %s. Le paquet est recomposé sans "
                     "ton lot ; corrige puis regèle, il reviendra à une coupure "
                     "suivante. %s"
                     % (lot, name, run_id, frozen_sha(item)[:12], ", ".join(verdict.tests),
                        run.get("url") or ""))
    ctx.mail(ctx.pilot, carrier,
             "#%d %s : ci-full rouge (run %s), retrait de %s (%s) ; recomposition."
             % (carrier, name, run_id, removed, ", ".join(verdict.tests)))
    result = paquet_auto(ctx, name, carrier, exclude=verdict.lots, force=True)
    return after_compose(ctx, carrier, name, result, step="recomposition")


def tick(ctx: Context) -> str:
    rows = open_lots(ctx)
    carriers = [row for row in rows if is_carrier(row, ctx.project, ctx.target)]
    if carriers:
        own = [row for row in carriers if is_hourly(row)
               and "paquet-horaire" in str(row.get("source") or "")]
        if len(own) == 1 and len(carriers) == 1:
            return follow(ctx, show(ctx, int(own[0]["id"])))
        return "%s réservée par %s : rien à faire" % (
            ctx.target, ", ".join("#%d" % int(row["id"]) for row in carriers))
    return compose(ctx, recent_lots(ctx))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Paquet horaire déclenché par minuterie, sans agent (#3845).")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("tick", help="un tic : composer, publier ou suivre")
    one.add_argument("--repo", default=".", help="clone du projet (défaut : .)")
    one.add_argument("--target", default="main")
    one.add_argument("--project", default="ameesh")
    one.add_argument("--pilot", default="mesh-design",
                     help="destinataire des courriers et assigné du porteur")
    one.add_argument("--actor", default=None,
                     help="auteur des gestes (défaut : AMEESH_ROLE, sinon --pilot)")
    one.add_argument("--python", default=None,
                     help="interpréteur des scripts enfants (défaut : celui-ci)")
    one.add_argument("--dry-run", action="store_true",
                     help="lire et dire, sans créer, pousser, noter ni écrire")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print("paquet-horaire : --repo %s n'est pas un clone" % repo, file=sys.stderr)
        return 2
    actor = args.actor or os.environ.get("AMEESH_ROLE") or args.pilot
    ctx = Context(repo=repo, target=args.target, project=args.project,
                  pilot=args.pilot, actor=actor, shell=Shell(),
                  python=args.python or auto.project_python(repo),
                  dry_run=args.dry_run)
    try:
        print(tick(ctx), flush=True)
        return 0
    except InputError as error:
        print("paquet-horaire : %s" % error, file=sys.stderr)
        return 2
    except (HoraireError, auto.PaquetAutoError) as error:
        print("paquet-horaire : %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
