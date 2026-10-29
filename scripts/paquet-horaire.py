#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Paquet horaire déclenché par minuterie, sans agent (#3845, #1606 point 1).

    scripts/paquet-horaire.py tick --repo ~/ameesh-composeur --target main
    scripts/paquet-horaire.py pause --until 20:15 --reason "staging hors créneau"
    scripts/paquet-horaire.py resume | status

`scripts/paquet-auto.py` (#3807) compose sans jugement, mais un agent devait
encore le lancer chaque heure, créer le lot porteur, poser « verdict non
requis », demander la ci-full, puis, sur un rouge, trouver l'unité fautive et
recomposer (h-32 : bisect à la main pour #3779). Ce script fait ces gestes, un
**tic** à la fois, sans état local : tout ce qu'il sait vient de la base
(lots, journal du porteur) et de la forge, si bien qu'un tic perdu, un hôte
arrêté ou un bail repris par un autre candidat ne laisse rien en suspens.

Avant tout geste, le tic pose les migrations **additives** de la tête que la
base partagée n'a pas encore (#4075) : sinon le code de la tête lit des
colonnes absentes. Une migration non additive en attente, ou une pose refusée
(droit DDL) : aucun geste, un lot d'empreinte, le pilote et le bureau prévenus.

Un tic (la minuterie `deploy/systemd/ameesh-paquet-horaire.timer`, sous
`ameesh role run`, ne le lance que sur l'hôte qui détient le bail) :

1. **Réservation (§12).** Un porteur de paquet ouvert sur la cible (branche
   `integration/paquet-*` ou titre « Paquet horaire h-… ») la réserve : le tic
   suit ce porteur s'il est à lui (étapes 3-4), sinon ne fait rien.
2. **Composition, au moins une fois par heure.** Cible libre et aucun porteur
   horaire créé dans l'heure locale (fuseau du canon compilé) dont la fusion
   ne soit pas vue (#4171 : un porteur fusionné libère le créneau, un porteur
   clos sans fusion le garde) : la liste calculée est lue
   (`paquet-candidats.py` depuis un worktree à la tête exacte de la cible) ;
   sans lot prêt, rien n'est créé. Sinon le lot porteur « Paquet horaire
   h-AAAAMMJJ-NN » est créé et `paquet-auto.py` compose, pousse et gèle.
3. **Publication.** Sur la tête gelée sans demande `ci-full` : « verdict non
   requis » **seulement** si aucune unité n'est une résolution (fusions
   propres, §2 : aucun avis) — avec une résolution le geste reste au pilote
   (#1979) et le courrier le dit ; puis `ameesh ci run <porteur> --full`, qui
   arme la fusion automatique (`packet_automerge`, tic de release).
4. **Rouge.** À la composition, un test ciblé rouge est attribué par la même
   lecture que #3801 (`packet_red`) : l'unité désignée par une preuve forte
   est retirée, son auteur prévenu, et `paquet-auto.py` recompose sans elle
   dans le même lancement (#3931) — la tentative n'est pas perdue. Sans
   attribution sûre, `paquet-auto.py` garde son échec antérieur (contrat rouge
   ou reprise bornée) et le pilote est prévenu. Sur la ci-full rouge de la tête
   gelée : un rouge **déterministe** (chaque échec de test propre au paquet
   attribué par #3801 à une seule unité probable, rejoué rouge en local sur la
   tête) retire ces unités ; le paquet est recomposé sans elles
   (`paquet-auto.py --force --exclude`), **une fois**, et l'auteur de chaque
   unité retirée reçoit un courrier. Chaque lot retiré repasse en `build`
   avec sa trace (`paquet-horaire: exclu …` : paquet, porteur, run, tests ;
   #4028), sans consommer de boucle d'auteur (#4043) : il attend le correctif
   de son assigné jusqu'à un nouveau gel, et l'alerte SLO vise l'auteur, pas
   le pilote. Un rouge non concluant (§3 : run annulé ou
   hors délai, échec de job sans test, échec non attribuable ou ambigu, test
   vert en local) ne retire rien : il est consigné au journal du porteur et le
   pilote est prévenu, une fois.
5. **Reprise.** Chaque lancement de `paquet-auto.py` laisse d'abord une
   marque « tentative » (étape, tête de départ, hôte:pid, époque du bail) ;
   son achèvement se lit à la tête gelée qui a bougé, son échec à une marque
   « échec ». Une tentative morte (échec, processus disparu sur cet hôte,
   époque d'un autre hôte différente de celle du tic — son `packet record`
   est clôturé par l'époque, #4087 —, ou, sans époque, délai de composition
   dépassé ailleurs) est relancée sur le même porteur avec
   `--force`, au plus 3 fois, puis le pilote est prévenu une fois. Un porteur
   créé sans tentative (arrêt entre `work add` et la marque) est repris de même.
   #4237 : si la tentative morte a laissé une **tête testée** (refus de geste
   après des tests verts, `paquet-auto: tête testée …` au journal), la reprise
   la publie (`paquet-auto.py --publish`) sans recomposer ; une chaîne refusée
   (cible bougée) recompose dans le même tic.
   #4329 : un verrou `flock` par nom de paquet, tenu par `paquet-auto.py` de
   `clear_local_branch` à la fin, est relu avant toute reprise. Une composition
   lancée hors du tic est ainsi « en cours » (`composition en cours
   (hôte:pid)`) au lieu de laisser le tic supprimer son worktree vivant ; le
   verrou tombe avec le processus, une tentative morte reste reprise.
   #4501 : la vie d'une tentative sur cet hôte se lit au **pid vivant**, jamais
   à la ligne de commande (une recomposition peut tourner sous un autre nom) ;
   et si une tête poussée du porteur (gelée, ou vue en avance) a une ci-full en
   cours, la reprise attend sa conclusion au lieu de recomposer et d'écraser
   son manifeste.
6. Un courrier au pilote à chaque étape (tête, unités, écartés, CI).

**Recomposition du pilote** (#4237) : `paquet-horaire.py recompose --carrier N
--exclude LOT [--exclude-reason TEXTE]` au lieu de `paquet-auto.py --force` à
la main : la marque « tentative » (hôte, pid, époque de la session, motifs) est
posée d'abord, si bien qu'un tic du détenteur courant reprend une recomposition
perdue à la relève du bail comme les siennes. Elle ne compte pas dans le
retrait automatique unique du point 4.

#3256 C : quand la médiane d'attente `qa_merge` de la veille dépasse
`paquet.attente_mediane_s` (bloc du canon compilé, lu avec `ameesh debit
--json --no-jobs`), le tic ouvre **deux** porteurs dans la même heure. Les
lots prêts sont partagés par `ameesh.packet_parallel` : aucun fichier modifié
commun, et deux lots dont l'un dépend de l'autre dans le même groupe. Chaque
porteur compose avec `paquet-auto.py --only <son groupe>` ; les deux CI
partent ensemble et `packet_automerge` fusionne le second après le premier,
sans refaire la suite complète quand la fusion de sa tête dans la cible
avancée est propre et les contrôles de gel verts. Un conflit le rend à la
file. Sous le seuil, ou si les lots ne sont pas disjoints, un seul paquet
part.

**Pause (#4361).** `pause --until … --reason …` suspend le tic (staging hors
créneau) sans arrêter la minuterie : `role timers --check` reste vert et nomme
la pause. L'échéance est obligatoire (au plus 12 h) ; la pause vit au journal
partagé (`packet.pause`, `ameesh.packet_pause`), pas sur l'hôte : un bail qui
change de main la garde. Pendant la pause, le tic ne fait aucun geste (ni
migration ni composition) et le dit ; à l'échéance il reprend seul ; `resume`
lève avant. Pose et levée sont dites au pilote et au bureau.

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
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TITLE_PREFIX = "Paquet horaire "
HOURLY_RE = re.compile(r"^Paquet horaire (h-(\d{8})-(\d+))\b")
CARRIER_BRANCH_PREFIX = "integration/paquet"
OPEN_STATES = ("intake", "build", "qa", "validated", "blocked", "waiting_human")
#: #4171 : fusion vue d'un porteur (miroir de `work_lifecycle.DELIVERED` et
#: des raisons de clôture livrée de `work.CLOSE_REASONS`).
MERGED_STATES = ("merged", "promoted")
MERGED_CLOSE_REASONS = ("delivered", "done")
MARK = "paquet-horaire: "
MARK_CI = MARK + "ci demandée "
MARK_RED = MARK + "rouge consigné "
MARK_WAIT = MARK + "verdict au pilote "
MARK_ATTEMPT = MARK + "tentative "
MARK_FAILED = MARK + "échec "
MARK_STUCK = MARK + "reprise épuisée "
#: #4245 : un refus de la garde d'époque (EP4) n'est pas un échec de
#: composition (§3, non concluant) : il ne consomme pas de tentative
MARK_FENCED = MARK + "non concluant "
MARK_EXCLUDED = MARK + "exclu "
COMPOSITION = "composition"
RECOMPOSITION = "recomposition"
ATTEMPT_RE = re.compile(
    r"^paquet-horaire: tentative (composition|recomposition) n=(\d+) base=(\S+) "
    r"par=(\S+):(\d+)(?: run=(\S+) sans=([\d,]*))?(?: epoque=(\d+))?"
    r"(?: publie=(\S+))?(?: motifs=(\[.*\]))?$")
#: #4237 : le `run=` d'une recomposition demandée par le pilote (sous-commande)
PILOT_RUN = "pilote"
FAILED_RE = re.compile(r"^paquet-horaire: échec (composition|recomposition) n=(\d+) base=(\S+)")
FENCED_RE = re.compile(
    r"^paquet-horaire: non concluant (composition|recomposition) n=(\d+) base=(\S+)")
#: #4245 : le refus de `pilot_gesture` d'une session dont l'époque n'est plus
#: celle du bail (et que la lignée au même hôte ne couvre pas)
FENCE_REFUSAL_RE = re.compile(r"refused for \S+: this session is fenced by the lease epoch")
#: #4245 : relances après refus de garde pour une même étape, au-delà
#: desquelles un agent lit le journal (une garde qui refuse toujours)
MAX_FENCED = 3
MAX_RECOMPOSITIONS = 1
#: #4201 : motif écrit au manifeste pour le retrait automatique d'une unité dont
#: la ci-full attribuée est rouge (`paquet-auto` seul écrit le motif neutre).
EXCLUDE_CI_FULL_REASON = "retiré : ci-full rouge attribuée à son unité (#3801)"
#: lancements de paquet-auto pour une même étape (même tête de départ)
MAX_ATTEMPTS = 3
#: un porteur sans tentative plus jeune que ce délai : création en cours
CREATION_GRACE = 300
#: #4113, mesure auditeur #153989 du 28/10 : médiane 164 min, 112 paquets.
CARRIER_ESTIMATE = "165m"
CARRIER_ESTIMATE_SOURCE = "auditeur #153989, 28/10/2026 : médiane 164 min sur 112 paquets (#4113)"
CREATED_RE = re.compile(r"^lot #(\d+) created", re.MULTILINE)
SCRIPT_DIR = Path(__file__).resolve().parent
LIST_LIMIT = 300
COMMAND_TIMEOUT = 600
COMPOSE_TIMEOUT = 3 * 3600
#: au-delà, une tentative lancée sur un autre hôte sans époque est réputée morte
ATTEMPT_STALE = COMPOSE_TIMEOUT + 900
REPLAY_TIMEOUT = 900
#: poids #3801 d'une preuve forte (fichier du test, liste, migration) :
#: `packet_red.attribute_tests` rend `best` = poids de la meilleure preuve
STRONG_EVIDENCE = 4


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
    #: fuseau des coupures, lu au canon compilé (`mechanics.timezone`, #2449)
    zone: ZoneInfo = ZoneInfo("UTC")
    #: #4271 : `packet_tests` du canon compilé (borne d'une série d'unité,
    #: séries simultanées, #4463 borne totale) ; None : bloc absent, les
    #: défauts de paquet-auto
    unit_tests: object = None
    now: float = field(default_factory=time.time)
    host: str = field(default_factory=socket.gethostname)
    pid: int = field(default_factory=os.getpid)
    #: époque du bail du tic (`AMEESH_LEASE_EPOCH`, `ameesh role run`) ; 0 : inconnue
    epoch: int = 0
    #: #3256 C : le bloc `paquet` du canon compilé (`attente_mediane_s`,
    #: `max_paquets`) ; None = aucun second paquet (bloc absent).
    parallel: object | None = None
    #: l'inventaire calculé du tic, gardé pour ne pas relire la cible
    candidates_cache: dict = field(default_factory=dict)
    #: #4361 : la pause active de la cible (`packet_pause`), None sans pause ;
    #: None ici : aucune lecture (tests)
    pause_of: object = None
    log: list[str] = field(default_factory=list)

    def alive(self, pid: int) -> bool:
        """Un processus vit-il encore sous ce pid, sur cet hôte ? (#4501)

        La ligne de commande ne prouve rien : une recomposition peut tourner
        sous un autre nom (copie `scripts/.h12-tick.py`, wrapper de session) et
        un pid vivant jugé mort fait recomposer le même porteur en parallèle.
        On lit donc l'état du processus (`/proc/<pid>/stat`, un zombie ne
        compte pas) ; hors Linux (`/proc` absent), `os.kill(pid, 0)`. Un pid
        recyclé reste un doute, jamais une fausse mort : le verrou de
        composition et le bail tranchent.
        """
        if pid <= 0:
            return False
        if os.path.isdir("/proc"):
            try:
                text = Path("/proc/%d/stat" % pid).read_text(
                    encoding="ascii", errors="replace")
            except FileNotFoundError:
                return False
            except OSError:
                # procfs lisible en général mais pas pour ce pid (droits,
                # course) : ne pas conclure à une mort sans le dire.
                return self._alive_by_signal(pid)
            # champ 3 = état ; le nom (champ 2) peut contenir espaces et
            # parenthèses, on coupe après la dernière.
            try:
                state = text.rsplit(")", 1)[1].split()[0]
            except (IndexError, ValueError):
                return True
            return state != "Z"
        return self._alive_by_signal(pid)

    @staticmethod
    def _alive_by_signal(pid: int) -> bool:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

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
        # #3845 : `ameesh role run` retire AGENT_MAIL_NAME ; l'expéditeur est
        # l'acteur (le rôle), nommé explicitement.
        result = self.shell.run(["agent-mail", "send", to, "-", "--lot", str(lot),
                                 "--from", self.actor], input_text=body, timeout=120)
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


def hour_start(now: float, zone: ZoneInfo) -> float:
    local = datetime.fromtimestamp(now, zone)
    return local.replace(minute=0, second=0, microsecond=0).timestamp()


def merged_seen(row: dict) -> bool:
    """#4171 : la fusion du porteur est-elle vue (état livré, ou clos livré) ?"""
    state = str(row.get("state") or "")
    return state in MERGED_STATES or (
        state == "closed" and str(row.get("close_reason") or "") in MERGED_CLOSE_REASONS)


def slot_served(rows: list[dict], now: float, zone: ZoneInfo, project: str,
                target: str) -> bool:
    """Un porteur horaire créé dans l'heure locale courante consomme-t-il le créneau ?

    #4171 (décision #154026 a) : un porteur dont la fusion est vue ne le
    consomme plus, le suivant se compose au tic suivant ; un porteur clos sans
    fusion (abandonné, rien composé) le garde, pour ne pas recomposer à
    chaque tic. L'heure reste le rythme minimum.
    """
    start = hour_start(now, zone)
    return any(is_hourly(row) and is_carrier(row, project, target)
               and float(row.get("created_ts") or 0) >= start
               and not merged_seen(row) for row in rows)


def next_name(ctx: Context) -> str:
    """h-AAAAMMJJ-NN, NN = suivant le plus grand du jour (branches d'origin)."""
    day = datetime.fromtimestamp(ctx.now, ctx.zone).strftime("%Y%m%d")
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


def ci_in_progress(ctx: Context, item: dict) -> dict | None:
    """#4501 : le run en cours de la tête poussée du porteur, ou None.

    La tête poussée est la tête gelée et/ou la tête de branche vue en avance
    (`branch_head`, relevé partagé) : une poussée plus récente, pas encore
    gelée, est déjà sous CI. Un porteur dont une de ces têtes est sous ci-full
    n'est ni recomposé ni réenregistré par un second composeur : la reprise
    attend la conclusion du run. Une lecture impossible de `ci status` lève
    (aucun geste), elle ne vaut jamais « aucun run ».
    """
    heads = {str(item.get("branch_head") or "").strip().lower(), frozen_sha(item)}
    heads.discard("")
    if not heads:
        return None
    report = ci_status(ctx, int(item["id"]))
    for run in report.get("runs") or []:
        if str(run.get("head_sha") or "").lower() in heads \
                and str(run.get("status") or "") in (
                    "running", "queued", "pending", "in_progress"):
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
                exclude: list[int] = (), exclude_reasons: list[str] = (),
                only: list[int] = (), force: bool = False,
                publish: str = "") -> Result:
    """`publish` : « réf@sha » de la tête testée (#4237), publiée sans retester."""
    cmd = [ctx.python, str(SCRIPT_DIR / "paquet-auto.py"), "--repo", str(ctx.repo),
           "--target", ctx.target, "--name", name, "--carrier", str(carrier),
           "--pilot", ctx.pilot, "--from", ctx.actor]
    if publish:
        ref, _, sha = publish.rpartition("@")
        cmd += ["--publish", ref, "--publish-sha", sha]
    if force:
        cmd.append("--force")
    if ctx.unit_tests is not None:
        cmd += ["--tests-timeout", str(ctx.unit_tests.unit_timeout_seconds),
                "--tests-parallel", str(ctx.unit_tests.parallel)]
        total = getattr(ctx.unit_tests, "total_timeout_seconds", None)
        if total:
            cmd += ["--tests-budget", str(total)]
        else:
            ctx.say("packet_tests.total_timeout_seconds absent du canon "
                    "compilé : tests par unité sans borne totale (#4463)")
    else:
        ctx.say("packet_tests absent du canon compilé : tests ciblés en série, "
                "borne par défaut de paquet-auto (#4271)")
    for lot in only:
        cmd += ["--only", str(lot)]
    for lot in exclude:
        cmd += ["--exclude", str(lot)]
    for reason in exclude_reasons:
        cmd += ["--exclude-reason", reason]
    if ctx.dry_run:
        ctx.say("[essai] %s" % " ".join(cmd))
        return Result(0, "aucun lot prêt : rien à composer (essai)\n", "")
    return ctx.shell.run(cmd, cwd=ctx.repo, timeout=COMPOSE_TIMEOUT)


def candidates(ctx: Context) -> dict:
    """L'inventaire calculé, lu depuis un worktree à la tête exacte de la cible.

    Le résultat est gardé sur le contexte (`candidates_cache`) : la
    composition et le partage #3256 C lisent la même liste, sans refaire un
    worktree ni relire la cible.
    """
    if ctx.candidates_cache:
        return ctx.candidates_cache
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
        payload["base"] = base
        ctx.candidates_cache = payload
        return payload
    finally:
        ctx.git("worktree", "remove", "--force", str(path))
        shutil.rmtree(path, ignore_errors=True)


def ready_lots(ctx: Context) -> list[int]:
    """Les identifiants prêts de l'inventaire calculé (jamais un courrier)."""
    payload = candidates(ctx)
    errored = {int(item.get("work_item") or item.get("lot") or 0)
               for item in payload.get("errors_by_lot") or []}
    return [int(lot) for lot in payload.get("ready") or [] if int(lot) not in errored]


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
        units = entry.get("units") or []
        top = max((int(unit.get("best") or 0) for unit in units), default=0)
        best = [unit for unit in units if int(unit.get("best") or 0) == top]
        if top < STRONG_EVIDENCE:
            return Verdict(False, [], [], "%s : preuve faible (poids %d)" % (name, top))
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

#: #3256 C : la trace qui fait d'un porteur le second d'une paire parallèle.
SECOND_PREFIX = "paquet-horaire: second paquet parallèle de #"
#: le groupe de lots composé dans ce porteur (les paires et les reprises)
GROUP_RE = re.compile(r"groupe=(\d+(?:,\d+)*)")
SECOND_RE = re.compile(r"^paquet-horaire: second paquet parallèle de #(\d+)\b")


def carrier_group(item: dict) -> list[int]:
    """Les lots que ce porteur doit composer (`groupe=…` de sa note)."""
    for event in reversed(_events(item)):
        match = GROUP_RE.search(str(event.get("note") or ""))
        if match:
            return [int(lot) for lot in match.group(1).split(",") if lot]
    return []


def second_peer(item: dict) -> int | None:
    """Le porteur du premier paquet, si ce porteur est le second d'une paire."""
    for event in reversed(_events(item)):
        match = SECOND_RE.match(str(event.get("note") or ""))
        if match:
            return int(match.group(1))
    return None


def yesterday_measure(ctx: Context) -> dict | None:
    """La section `qa_merge` du débit (#3256 C), lue sans jobs GitHub.

    Une lecture impossible n'ouvre rien : le tic continue avec un seul paquet,
    et le dit. Le débit est la seule source de la mesure ; le seuil, lui,
    vient du canon compilé (`paquet.attente_mediane_s`).
    """
    if ctx.parallel is None:
        return None
    try:
        result = ctx.ameesh("debit", "--json", "--no-jobs", timeout=COMMAND_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - une mesure absente ne bloque pas
        ctx.say("paquet parallèle : débit illisible (%s), un seul paquet" % exc)
        return None
    if result.returncode != 0:
        ctx.say("paquet parallèle : débit rc %d (%s), un seul paquet"
                % (result.returncode, auto._tail(result.text, 5)))
        return None
    try:
        return _json(result, "debit").get("qa_merge") or {}
    except HoraireError as exc:
        ctx.say("paquet parallèle : débit illisible (%s), un seul paquet" % exc)
        return None


def parallel_decision(ctx: Context) -> dict | None:
    """Le second paquet est-il ouvert par la médiane de la veille ?"""
    if ctx.parallel is None:
        return None
    from ameesh import packet_parallel
    decision = packet_parallel.trigger(
        yesterday_measure(ctx), getattr(ctx.parallel, "attente_mediane_s", None),
        now=ctx.now, zone=ctx.zone)
    if not decision.get("enabled"):
        ctx.say("paquet : un seul paquet (%s)" % decision.get("why"))
        return None
    return decision


def parallel_partition(ctx: Context, ready: list[int]) -> dict | None:
    """Partage les lots prêts en deux groupes disjoints, ou None."""
    try:
        payload = candidates(ctx)
    except HoraireError as exc:
        ctx.say("paquet parallèle : inventaire illisible (%s), un seul paquet" % exc)
        return None
    by_lot = {int(entry.get("work_item") or 0): entry
              for entry in payload.get("candidates") or []}
    entries = [by_lot[lot] for lot in ready if lot in by_lot]
    from ameesh import packet_parallel
    return packet_parallel.partition(entries, ready)


def _create_carrier(ctx: Context, name: str, group: list[int], *,
                    parallel: int) -> int:
    branch = auto.package_branch(name)
    title = "%s%s — composition automatique (§11 bis, §12)" % (TITLE_PREFIX, name)
    detail = ""
    if parallel:
        detail = (" Paquet parallèle #3256 C (%d/%d) : groupe disjoint %s."
                  % (parallel, 2, ", ".join("#%d" % lot for lot in group)))
    created = ctx.must(
        "work", "add", "--title", title, "--type", "evolution",
        "--app", ctx.project, "--assignee", ctx.pilot, "--priority", "1",
        "--branch", branch, "--target", ctx.target,
        "--estimate", CARRIER_ESTIMATE,
        "--estimate-source", CARRIER_ESTIMATE_SOURCE,
        "--source", "paquet-horaire (#3845), minuterie",
        "--body", "Paquet horaire composé sans agent par scripts/paquet-horaire.py "
                  "(#3845) : %d lot(s) prêt(s) à la lecture (%s).%s"
                  % (len(group), ", ".join("#%d" % lot for lot in group), detail),
        "--actor", ctx.actor, what="création du porteur")
    found = CREATED_RE.search(created.stdout)
    if not found:
        raise HoraireError("porteur créé sans identifiant lisible : %s"
                           % auto._tail(created.text, 5))
    carrier = int(found.group(1))
    ctx.say("porteur #%d : %s" % (carrier, title))
    return carrier


def compose(ctx: Context, rows: list[dict]) -> str:
    if slot_served(rows, ctx.now, ctx.zone, ctx.project, ctx.target):
        return "porteur horaire déjà créé dans l'heure, non fusionné : rien à faire"
    ready = ready_lots(ctx)
    if not ready:
        return "aucun lot prêt sur %s : rien à faire" % ctx.target
    groups = [ready]
    parallel = parallel_decision(ctx)
    if parallel is not None:
        plan = parallel_partition(ctx, ready)
        if plan and plan.get("second"):
            groups = [plan["first"], plan["second"]]
            ctx.say("paquets parallèles #3256 C : %s | %s (médiane %s s > seuil %s s)"
                    % (", ".join("#%d" % lot for lot in groups[0]),
                       ", ".join("#%d" % lot for lot in groups[1]),
                       parallel.get("median_s"), parallel.get("threshold_s")))
        else:
            ctx.say("paquet parallèle impossible : les lots ne sont pas disjoints, "
                    "un seul paquet")
    if ctx.dry_run:
        for index, group in enumerate(groups, start=1):
            ctx.say("[essai] paquet %d/%d, %d lot(s) prêt(s) : %s"
                    % (index, len(groups), len(group),
                       ", ".join("#%d" % lot for lot in group)))
        return "essai : %d paquet(s)" % len(groups)
    first: int | None = None
    results: list[str] = []
    for index, group in enumerate(groups):
        name = next_name(ctx)
        # #4329 : une composition du même paquet vit (lancement à la main ou
        # tick) ; ne pas créer un porteur qu'elle refuserait.
        holder = composition_in_progress(ctx, name)
        if holder:
            results.append("composition en cours (%s)" % holder)
            continue
        carrier = _create_carrier(ctx, name, group,
                                  parallel=(index + 1 if len(groups) == 2 else 0))
        if len(groups) == 2:
            if index == 0:
                first = carrier
                ctx.note(carrier, "paquet-horaire: parallèle premier groupe=%s"
                         % ",".join(str(lot) for lot in group))
            elif first is not None:
                ctx.note(carrier, "%s%d groupe=%s"
                         % (SECOND_PREFIX, first,
                            ",".join(str(lot) for lot in group)))
        results.append(attempt(ctx, carrier, name,
                               Attempt(COMPOSITION, 1, "",
                                       only=(list(group) if len(groups) == 2 else []))))
    return "\n".join(results)


# ---------------------------------------------------------------------------
# Tentatives et reprise
# ---------------------------------------------------------------------------


@dataclass
class Attempt:
    step: str
    n: int
    base: str  # tête gelée au départ ("" : aucune)
    host: str = ""
    pid: int = 0
    run_id: str = ""
    exclude: list[int] = field(default_factory=list)
    #: #3256 C : le groupe que ce porteur compose (vide = toute la liste prête)
    only: list[int] = field(default_factory=list)
    ts: float = 0.0
    failed: bool = False
    #: #4245 : arrêtée par un refus de garde d'époque (non concluant, §3)
    fenced: bool = False
    #: #4245 (B impl-04) : reprise d'un refus de garde : `--force` même à n=1,
    #: la branche et le worktree locaux de l'essai refusé sont restés
    force: bool = False
    epoch: int = 0  # époque du bail à l'entrée (0 : marque sans époque)
    #: #4237 : « réf@sha » de la tête testée publiée par cette tentative ("" : composer)
    publish: str = ""
    #: #4237 : motifs d'exclusion du pilote, appariés à `exclude`
    reasons: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return "%s base=%s" % (self.step, self.base or "-")


def _events(item: dict) -> list[dict]:
    """Les événements du plus ancien au plus récent (`work show` : récents d'abord)."""
    events = list(item.get("events") or [])
    return sorted(events, key=lambda ev: (float(ev.get("created_ts") or 0),
                                          int(ev.get("id") or 0)))


def attempts(item: dict) -> list[Attempt]:
    """Les tentatives du journal, dans l'ordre, chacune marquée si un échec a suivi."""
    found: list[Attempt] = []
    for ev in _events(item):
        note = str(ev.get("note") or "")
        match = ATTEMPT_RE.match(note)
        if match:
            step, n, base, host, pid, run_id, without, epoch, ref, why = match.groups()
            try:
                reasons = [str(text) for text in json.loads(why)] if why else []
            except ValueError:
                reasons = []
            found.append(Attempt(
                step, int(n), "" if base == "-" else base, host, int(pid), run_id or "",
                [int(lot) for lot in (without or "").split(",") if lot],
                ts=float(ev.get("created_ts") or 0), epoch=int(epoch or 0),
                publish=ref or "", reasons=reasons))
            continue
        fenced = FENCED_RE.match(note)
        match = fenced or FAILED_RE.match(note)
        if match:
            step, n, base = match.groups()
            base = "" if base == "-" else base
            for previous in reversed(found):
                if (previous.step, previous.n, previous.base) == (step, int(n), base):
                    previous.failed = True
                    previous.fenced = bool(fenced)
                    break
    return found


def composition_in_progress(ctx: Context, name: str) -> str:
    """« hôte:pid » du verrou vivant de `name`, ou "" (#4329)."""
    holder = auto.composition_holder(ctx.repo, name)
    return auto.holder_label(*holder) if holder is not None else ""


def attempt(ctx: Context, carrier: int, name: str, this: Attempt) -> str:
    """Lancer paquet-auto pour une étape, la marque d'intention d'abord."""
    holder = composition_in_progress(ctx, name)
    if holder:
        # #4329 : une composition du même paquet vit (tick ou lancement à la
        # main) ; ne pas écrire une tentative que son verrou va refuser.
        return "#%d : composition en cours (%s)" % (carrier, holder)
    mark = "%s%s n=%d base=%s par=%s:%d" % (MARK_ATTEMPT, this.step, this.n,
                                            this.base or "-", ctx.host, ctx.pid)
    if this.step == RECOMPOSITION:
        mark += " run=%s sans=%s" % (this.run_id, ",".join(str(lot) for lot in this.exclude))
    if ctx.epoch:
        mark += " epoque=%d" % ctx.epoch
    if this.publish:
        mark += " publie=%s" % this.publish
    if this.reasons:
        mark += " motifs=%s" % json.dumps(this.reasons, ensure_ascii=False)
    ctx.note(carrier, mark)
    if this.publish:
        result = paquet_auto(ctx, name, carrier, publish=this.publish)
        return after_compose(ctx, carrier, name, result, this)
    if this.reasons:
        reasons = list(this.reasons)
    elif this.step == RECOMPOSITION and this.run_id != PILOT_RUN:
        reasons = [EXCLUDE_CI_FULL_REASON] * len(this.exclude)
    else:
        reasons = []
    # une reprise (n > 1) réécrit une branche peut-être déjà poussée
    result = paquet_auto(ctx, name, carrier, exclude=this.exclude,
                         exclude_reasons=reasons,
                         only=this.only,
                         force=this.step == RECOMPOSITION or this.n > 1 or this.force)
    return after_compose(ctx, carrier, name, result, this)


def frozen_ts(item: dict) -> float:
    freeze = item.get("freeze") or {}
    declared = freeze.get("declared") or {}
    return float(declared.get("at_ts") or 0)


def achieved(item: dict, sha: str, since: float) -> bool:
    """#4237 : la tête gelée `sha` est finie : publiée par le tic (ci demandée)
    ou dite verte par paquet-auto après ses tests par unité (depuis `since`)."""
    if MARK_CI + sha in marks(item):
        return True
    for ev in _events(item):
        if float(ev.get("created_ts") or 0) < since:
            continue
        match = auto.TESTS_DONE_RE.match(str(ev.get("note") or ""))
        if match and sha.startswith(match.group(1)):
            return True
    return False


def pending(ctx: Context, item: dict, sha: str) -> Attempt | None:
    """La dernière tentative, si elle n'a pas abouti.

    La tête gelée n'a pas bougé depuis sa marque ; ou (#4237, B cx1) elle a
    bougé vers la tête que la tentative a elle-même poussée et gelée avant ses
    tests (#4304 : gel postérieur à la marque, ou tête qu'elle publie), sans
    que cette tête soit finie (`achieved`) : refus de garde après les tests,
    échec ou arrêt en cours de tests, ou tentative encore vivante.
    """
    found = attempts(item)
    if found and sha:
        kept = tested_since(item, found[-1].ts)
        expected = (found[-1].publish.rsplit("@", 1)[-1]
                    if found[-1].publish else kept[1] if kept else "")
        # Un autre gel ne donne pas à cette tentative le droit de le remplacer.
        if expected and expected != sha and frozen_ts(item) > found[-1].ts:
            return None
    if found and found[-1].base == sha:
        return found[-1]
    if found and sha:
        last = found[-1]
        own = (frozen_ts(item) >= last.ts > 0
               or last.publish.endswith("@" + sha))
        # un gel posé après « reprise épuisée » est la main d'un agent
        stuck = [float(ev.get("created_ts") or 0) for ev in _events(item)
                 if str(ev.get("note") or "") == MARK_STUCK + last.key
                 and float(ev.get("created_ts") or 0) >= last.ts]
        if own and not achieved(item, sha, last.ts) \
                and not (stuck and frozen_ts(item) > stuck[-1]):
            return last
    if not sha and not found:
        # porteur créé, arrêt avant la première marque : une tentative 0 morte
        created = float(item.get("created_ts") or 0)
        if ctx.now - created < CREATION_GRACE:
            return Attempt(COMPOSITION, 0, "", ctx.host, ctx.pid, ts=created)
        return Attempt(COMPOSITION, 0, "", failed=True, ts=created)
    return None


def lease_epoch() -> int:
    """L'époque passée par `ameesh role run` ; 0 si absente ou illisible."""
    text = (os.environ.get("AMEESH_LEASE_EPOCH")
            or os.environ.get("AGENT_MESH_LEASE_EPOCH") or "").strip()
    try:
        return max(int(text), 0)
    except ValueError:
        return 0


def running(ctx: Context, this: Attempt, name: str = "") -> bool:
    if this.failed:
        return False
    # #4329 : un verrou vivant, même sans marque de tentative (composition
    # lancée hors du tic), fait compter la composition comme en cours.
    if name and composition_in_progress(ctx, name):
        return True
    if this.host == ctx.host:
        return this.pid == ctx.pid or ctx.alive(this.pid)
    if this.epoch and ctx.epoch:
        # le bail a changé de main : le `packet record` de l'ancienne époque
        # est refusé (EP4) avant la poussée, elle ne publie plus rien (#4087)
        return this.epoch == ctx.epoch
    return ctx.now - this.ts < ATTEMPT_STALE


def resume(ctx: Context, item: dict, this: Attempt) -> str:
    """Une tentative sans aboutissement : attendre, relancer, ou prévenir une fois."""
    carrier = int(item["id"])
    branch = str(item.get("branch") or "")
    name = auto.package_name(branch)
    holder = composition_in_progress(ctx, name) if name else ""
    if holder:
        # #4329 : le verrou du paquet prime le pid de la marque, qui peut être
        # celui d'une tentative morte.
        return "#%d : composition en cours (%s)" % (carrier, holder)
    if running(ctx, this, name):
        return "#%d : %s en cours (%s:%d)" % (carrier, this.step, this.host, this.pid)
    # #4501 : la tête poussée est sous ci-full ; un second composeur ne
    # recompose ni ne réenregistre par-dessus (le premier peut encore aboutir).
    pending_run = ci_in_progress(ctx, item)
    if pending_run is not None:
        return "#%d : ci-full en cours sur %s (run %s), reprise suspendue" % (
            carrier, frozen_sha(item)[:12], pending_run.get("run_id") or "?")
    if this.fenced:
        # #4245 : refus de garde, non concluant : même numéro de tentative,
        # borné par MAX_FENCED ; `--force` ferme la branche et le worktree
        # locaux laissés par l'essai refusé (le refus précède la poussée)
        fenced = sum(1 for a in attempts(item) if a.fenced and a.key == this.key)
        if fenced < MAX_FENCED:
            ctx.say("#%d : reprise de la %s après refus de garde (tentative %d "
                    "non décomptée)" % (carrier, this.step, this.n))
            # #4237 : une tête testée gardée depuis se publie sans recomposer
            kept = tested_since(item, this.ts)
            return attempt(ctx, carrier, name, Attempt(
                this.step, this.n, this.base, run_id=this.run_id,
                exclude=list(this.exclude), only=carrier_group(item), force=True,
                publish=("%s@%s" % (kept[2], kept[1]) if kept else this.publish),
                reasons=list(this.reasons)))
        if MARK_STUCK + this.key in marks(item):
            return "#%d : reprise de la %s épuisée, déjà signalée" % (carrier, this.step)
        ctx.mail(ctx.pilot, carrier,
                 "#%d %s : %s refusée %d fois par la garde d'époque (tête %s) ; "
                 "plus de reprise automatique, un agent lit le journal du porteur."
                 % (carrier, name, this.step, fenced, (this.base or "aucune")[:12]))
        ctx.note(carrier, MARK_STUCK + this.key)
        return "#%d : reprise de la %s épuisée (garde)" % (carrier, this.step)
    if this.n >= MAX_ATTEMPTS:
        if MARK_STUCK + this.key in marks(item):
            return "#%d : reprise de la %s épuisée, déjà signalée" % (carrier, this.step)
        ctx.mail(ctx.pilot, carrier,
                 "#%d %s : %s lancée %d fois sans aboutir (tête %s) ; plus de reprise "
                 "automatique, un agent lit le journal du porteur."
                 % (carrier, name, this.step, this.n, (this.base or "aucune")[:12]))
        ctx.note(carrier, MARK_STUCK + this.key)
        return "#%d : reprise de la %s épuisée" % (carrier, this.step)
    kept = tested_since(item, this.ts)
    if kept:
        ctx.say("#%d : reprise de la %s (tentative %d morte) par la tête testée %s"
                % (carrier, this.step, this.n, kept[1][:12]))
    else:
        ctx.say("#%d : reprise de la %s (tentative %d morte)" % (carrier, this.step, this.n))
    return attempt(ctx, carrier, name, Attempt(this.step, this.n + 1, this.base,
                                               run_id=this.run_id,
                                               exclude=list(this.exclude),
                                               only=carrier_group(item),
                                               publish=("%s@%s" % (kept[2], kept[1])
                                                        if kept else ""),
                                               reasons=list(this.reasons)))


def tested_since(item: dict, since: float) -> tuple[str, str, str] | None:
    """#4237 : la dernière tête testée gardée depuis `since` : (base, tête, réf).

    Écrite par `paquet-auto.keep_tested` sur un refus de geste après des tests
    verts ; une réf « - » (poussée échouée) n'est pas publiable. Une tentative
    de publication ne réécrit jamais cette marque : une publication morte se
    reprend en recomposant.
    """
    found = None
    for ev in _events(item):
        if float(ev.get("created_ts") or 0) < since:
            continue
        match = auto.TESTED_RE.match(str(ev.get("note") or ""))
        if match:
            found = match.groups() if match.group(3) != "-" else None
    return found


def after_compose(ctx: Context, carrier: int, name: str, result: Result,
                  this: Attempt) -> str:
    step = this.step
    # La sauvegarde Git réussit parfois alors que sa note échoue ; récupérer
    # la marque exacte de la sortie avant de journaliser le refus de garde.
    for line in result.stdout.splitlines():
        if auto.TESTED_RE.match(line):
            ctx.note(carrier, line)
    # N1 de relecture : la fin de la sortie seulement (l'erreur qui arrête
    # paquet-auto), pas une phrase citée plus haut par un test rouge
    if result.returncode != 0 and FENCE_REFUSAL_RE.search(auto._tail(result.text, 10)):
        # #4245 : la garde EP4 a refusé un geste (bail passé ailleurs, ou
        # lignée rompue) ; ni échec de composition ni tentative consommée
        ctx.note(carrier, "%s%s n=%d base=%s époque=%d" % (
            MARK_FENCED, step, this.n, this.base or "-", ctx.epoch))
        ctx.mail(ctx.pilot, carrier,
                 "#%d %s : %s refusée par la garde d'époque (époque %d, tentative "
                 "%d/%d non décomptée, §3 non concluant) ; le tic suivant reprend.\n\n%s"
                 % (carrier, name, step, ctx.epoch, this.n, MAX_ATTEMPTS,
                    auto._tail(result.text, 10)))
        return "#%d : %s non concluante (refus de garde d'époque)" % (carrier, step)
    if this.publish and result.returncode == auto.PUBLISH_REFUSED:
        if this.n < MAX_ATTEMPTS:
            # tête testée non publiable (cible bougée, chaîne) : recomposer tout de suite
            ctx.note(carrier, "%s%s n=%d base=%s rc=%d" % (
                MARK_FAILED, step, this.n, this.base or "-", result.returncode))
            ctx.say("#%d : tête testée refusée (%s), recomposition"
                    % (carrier, auto._tail(result.text, 3)))
            # #3256 C : la recomposition d'un porteur parallèle garde son groupe ;
            # le repli relit la note du porteur si la tentative ne le portait pas.
            only = list(this.only)
            if not only:
                try:
                    only = carrier_group(show(ctx, carrier))
                except HoraireError:
                    only = []
            return attempt(ctx, carrier, name, Attempt(step, this.n + 1, this.base,
                                                       run_id=this.run_id,
                                                       exclude=list(this.exclude),
                                                       only=only,
                                                       reasons=list(this.reasons)))
    elif result.returncode == auto.IN_PROGRESS_RC:
        # #4329 : le verrou du paquet est vivant ; ce n'est pas un échec de
        # composition et le tic n'écrit ni marque d'échec ni courrier.
        holder = composition_in_progress(ctx, name) or "verrou tenu"
        return "#%d : composition en cours (%s)" % (carrier, holder)
    if result.returncode != 0:
        ctx.note(carrier, "%s%s n=%d base=%s rc=%d" % (
            MARK_FAILED, step, this.n, this.base or "-", result.returncode))
        ctx.mail(ctx.pilot, carrier,
                 "#%d %s : paquet-auto.py a échoué (rc %d, %s, tentative %d/%d) ; "
                 "le tic suivant reprend.\n\n%s"
                 % (carrier, name, result.returncode, step, this.n, MAX_ATTEMPTS,
                    auto._tail(result.text, 30)))
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
    if re.search(r"^%s" % re.escape(auto.MERGED_LINE), result.stdout, re.MULTILINE):
        # #4322 : fusionné au vert pendant les tests par unité (§11 bis) ;
        # paquet-auto a noté le porteur et prévenu le pilote d'un rouge
        return "#%d %s : fusionné pendant les tests par unité, rien à publier" % (
            carrier, name)
    item = show(ctx, carrier)
    return publish(ctx, item)


def publish(ctx: Context, item: dict) -> str:
    """Tête gelée : « verdict non requis » si aucune résolution, puis ci-full."""
    carrier = int(item["id"])
    sha = frozen_sha(item)
    if not sha:
        return "#%d sans tête gelée : rien à publier" % carrier
    if merged_seen(item) or str(item.get("state") or "") == "closed":
        # #4322 : la ci-full précoce (#4304) a pu fusionner le porteur avant
        # le retour de paquet-auto ; ni verdict ni ci-full sur un porteur clos
        return "#%d déjà %s : rien à publier" % (
            carrier, "fusionné" if merged_seen(item) else "clos")
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
    name = auto.package_name(str(item.get("branch") or ""))
    if name:
        holder = composition_in_progress(ctx, name)
        if holder:
            # #4329 : une composition (tick ou lancement à la main) tient le
            # verrou du paquet ; ne rien publier, retirer ni recomposer avant.
            return "#%d : composition en cours (%s)" % (carrier, holder)
    sha = frozen_sha(item)
    left = pending(ctx, item, sha)
    if left is not None:
        return resume(ctx, item, left)
    if not sha:
        return "#%d sans gel ni tentative : rien à faire" % carrier
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
    # #4237 : une recomposition du pilote n'use pas le retrait automatique
    recomposed = len({a.run_id for a in attempts(item)
                      if a.step == RECOMPOSITION and a.run_id != PILOT_RUN})
    if verdict.deterministic and recomposed < MAX_RECOMPOSITIONS:
        return recompose(ctx, item, run, verdict)
    reason = verdict.why if not verdict.deterministic else \
        "retrait automatique épuisé (%d recomposition)" % recomposed
    # courrier puis marque : un arrêt entre les deux redit, il ne tait pas
    ctx.mail(ctx.pilot, carrier,
             "#%d %s : ci-full rouge (run %s, %s) ; rien retiré : %s. "
             "Un agent lit (ameesh ci status %d)."
             % (carrier, item.get("branch"), run_id, sha[:12], reason, carrier))
    ctx.note(carrier, MARK_RED + "%s : %s" % (run_id, reason))
    return "#%d : rouge consigné (%s)" % (carrier, reason)


def recompose(ctx: Context, item: dict, run: dict, verdict: Verdict) -> str:
    carrier = int(item["id"])
    branch = str(item.get("branch") or "")
    name = auto.package_name(branch)
    holder = composition_in_progress(ctx, name) if name else ""
    if holder:
        # #4329 : ne pas retirer d'unité ni écrire aux auteurs pour une
        # composition que le verrou dit déjà en cours.
        return "#%d : composition en cours (%s)" % (carrier, holder)
    run_id = str(run.get("run_id") or "?")
    removed = ", ".join("#%d" % lot for lot in verdict.lots)
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
        exclude(ctx, lot, carrier, name, run_id, verdict.tests)
    ctx.mail(ctx.pilot, carrier,
             "#%d %s : ci-full rouge (run %s), retrait de %s (%s) ; recomposition."
             % (carrier, name, run_id, removed, ", ".join(verdict.tests)))
    return attempt(ctx, carrier, name, Attempt(RECOMPOSITION, 1, frozen_sha(item),
                                               run_id=run_id,
                                               exclude=list(verdict.lots),
                                               only=carrier_group(item)))


def exclude(ctx: Context, lot: int, carrier: int, name: str, run_id: str,
            tests: list[str]) -> None:
    """#4028 : le lot retiré pour un test négatif porte sa propre trace et
    repasse en `build` : il attend le correctif de son assigné, et un nouveau
    gel le ramène en qa (puis au paquet). Sans cela il restait en qa, gelé avec
    son GO, et l'attente « merge » faisait réclamer au bureau son inclusion au
    paquet suivant. #4043 : le retrait n'est pas une boucle d'auteur
    (`--package-exclusion` : `loops` inchangé, jamais refusé à la limite). Une
    transition refusée (lot déjà sorti de qa) laisse la trace seule, en note."""
    trace = "%s%s porteur=#%d run=%s tests=%s : correctif dû par l'assigné, " \
            "regel pour revenir au paquet" % (MARK_EXCLUDED, name, carrier, run_id,
                                              ",".join(tests) or "?")
    if ctx.dry_run:
        ctx.say("[essai] #%d → build : %s" % (lot, trace))
        return
    moved = ctx.ameesh("work", "move", str(lot), "build", "--package-exclusion",
                       "--note", trace, "--actor", ctx.actor)
    if moved.returncode == 0:
        return
    ctx.say("#%d non repassé en build : %s" % (lot, auto._tail(moved.text, 5)))
    try:
        ctx.note(lot, trace)
    except HoraireError as exc:
        ctx.say("trace d'exclusion de #%d non écrite : %s" % (lot, exc))


def check_fresh(ctx: Context) -> None:
    """Le code exécuté est-il celui de la tête exacte de la cible ? Sinon, rien.

    Le `git pull` de la minuterie peut échouer (réseau, divergence, autre
    branche) : ce tic chargerait alors un ancien orchestrateur et écrirait
    quand même. Il faut donc ce script dans le clone, la tête locale égale à
    `origin/<cible>` tout juste lue, et aucun fichier suivi modifié.
    """
    if SCRIPT_DIR != (ctx.repo / "scripts").resolve():
        raise HoraireError("script lancé hors du clone %s (%s)" % (ctx.repo, SCRIPT_DIR))
    fetched = ctx.git("fetch", "--no-tags", "origin", ctx.target, timeout=300)
    if fetched.returncode != 0:
        raise HoraireError("fetch %s : %s" % (ctx.target, auto._tail(fetched.text, 5)))
    local = ctx.git("rev-parse", "HEAD^{commit}")
    remote = ctx.git("rev-parse", "origin/%s^{commit}" % ctx.target)
    if local.returncode or remote.returncode or not remote.stdout.strip():
        raise HoraireError("têtes illisibles (HEAD, origin/%s)" % ctx.target)
    if local.stdout.strip() != remote.stdout.strip():
        raise HoraireError("clone à %s, %s à %s : code périmé, aucun geste"
                           % (local.stdout.strip()[:12], ctx.target,
                              remote.stdout.strip()[:12]))
    dirty = ctx.git("status", "--porcelain", "--untracked-files=no")
    if dirty.returncode or dirty.stdout.strip():
        raise HoraireError("clone modifié : aucun geste\n%s" % auto._tail(dirty.text, 5))


#: `agent-mail migrate` chargé depuis `src` du clone (argv[1]), pas depuis le
#: paquet installé : ce sont les migrations de la tête qu'il faut poser.
MIGRATE_CODE = ("import sys; sys.path.insert(0, sys.argv[1]); from ameesh import cli; "
                "sys.exit(cli.main(['migrate', sys.argv[2]]))")


#: le bureau reçoit, avec le pilote, l'échec de pose (#4075)
BUREAU = "bureau"
MISSING_RE = re.compile(r"^(\d{4}_[A-Za-z0-9._-]+): (additive|NOT additive)", re.MULTILINE)
DENIED_RE = re.compile(r"permission denied|must be owner|insufficient.privilege|42501",
                       re.IGNORECASE)


def _migrate(ctx: Context, flag: str) -> Result:
    return ctx.shell.run([ctx.python, "-c", MIGRATE_CODE, str(ctx.repo / "src"), flag],
                         cwd=ctx.repo, timeout=COMMAND_TIMEOUT)


def align_schema(ctx: Context) -> None:
    """#4075 : la base partagée porte-t-elle les migrations de la tête ?

    Le tic et `paquet-candidats` exécutent le code de la tête exacte de la
    cible (#3782, `check_fresh`) contre la base partagée. Un paquet qui vient
    de fusionner une migration rend ce code incompatible avec la base tant que
    personne ne l'a posée (h-45 : « column operator_pause_until does not
    exist » à chaque tic, jusqu'à la pose manuelle de 0499). Le tic lit donc
    d'abord les migrations manquantes de la tête (`--dry-run`, lecture seule)
    et pose les **additives** (§11 bis : l'ancien code des exécuteurs reste
    compatible). Une migration non additive n'est jamais posée ici, et un
    refus de la base (droit DDL absent du rôle du tic) n'est pas contourné :
    aucun geste, l'échec nomme les migrations et la cause, et `alert_schema`
    prévient le pilote et le bureau. En essai, seule la lecture a lieu.

    Pas d'instantané avant pose : `migrations.migrate` pose toute la passe
    dans une seule transaction (verrou consultatif, `lock_timeout` 5 s) et
    refuse en bloc dès qu'une manquante n'est pas additive ; un échec ne
    laisse rien de posé, une passe réussie n'ajoute que des objets.
    """
    read = _migrate(ctx, "--dry-run")
    missing = MISSING_RE.findall(read.stdout)
    if read.returncode != 0:
        fail_schema(ctx, "lecture", [], read)
    if not missing:
        return
    labels = [label for label, _ in missing]
    refused = [label for label, kind in missing if kind != "additive"]
    if refused:
        fail_schema(ctx, "non-additive", labels, read)
    if ctx.dry_run:
        ctx.say("[essai] migrations à poser : %s" % ", ".join(labels))
        return
    result = _migrate(ctx, "--additive-only")
    if result.returncode != 0:
        kind = "droit-ddl" if DENIED_RE.search(result.text) else (
            "non-additive" if result.returncode == 3 else "pose")
        fail_schema(ctx, kind, labels, result)
    ctx.say("schéma : %s posée(s) sur la base partagée" % ", ".join(labels))


SCHEMA_WHY = {
    "lecture": "lecture des migrations impossible",
    "non-additive": "migration non additive en attente : jamais posée par le tic (§11 bis)",
    "droit-ddl": "le rôle DB du tic n'a pas le droit DDL",
    "pose": "pose refusée par la base",
}


def fail_schema(ctx: Context, kind: str, labels: list[str], result: Result) -> None:
    """Aucun geste ; pilote et bureau prévenus une fois par empreinte."""
    what = ", ".join(labels) or "?"
    message = ("schéma de la tête de %s non aligné (%s) : %s, %s : aucun geste\n%s"
               % (ctx.target, kind, what, SCHEMA_WHY[kind], auto._tail(result.text, 10)))
    alert_schema(ctx, kind, what, message)
    raise HoraireError(message)


def alert_schema(ctx: Context, kind: str, what: str, message: str) -> None:
    """Un lot par empreinte (`work fingerprint open`, #3885) : le tic suivant,
    même échec, note ce lot au lieu d'écrire de nouveau ; un nouvel échec ou
    une récidive après clôture ouvre un lot et écrit au pilote et au bureau.
    Sans lot lisible, le courrier part quand même : se taire est pire."""
    if ctx.dry_run:
        ctx.say("[essai] alerte schéma %s : %s" % (kind, what))
        return
    opened = ctx.ameesh(
        "work", "fingerprint", "open", "--app", ctx.project,
        "--key", "paquet-horaire:schema:%s:%s" % (kind, what),
        "--title", "Tic horaire bloqué : migrations %s non posées (%s)" % (what, kind),
        "--code", kind, "--actor", ctx.actor, "--json")
    lot = 0
    try:
        payload = json.loads(opened.stdout) if opened.returncode == 0 else {}
        if payload.get("created") is False:
            return
        lot = int(payload.get("lot") or 0)
    except (ValueError, TypeError, AttributeError):
        lot = 0
    for to in dict.fromkeys((ctx.pilot, BUREAU)):
        cmd = ["agent-mail", "send", to, "-", "--from", ctx.actor]
        if lot:
            cmd += ["--lot", str(lot)]
        sent = ctx.shell.run(cmd, input_text=message, timeout=120)
        if sent.returncode != 0:
            ctx.say("courrier à %s non envoyé : %s" % (to, auto._tail(sent.text, 5)))


def paused(ctx: Context) -> str | None:
    """#4361 : la pause bornée de la cible, lue au journal partagé ; une
    lecture impossible n'autorise aucun geste."""
    if ctx.pause_of is None:
        return None
    try:
        data = ctx.pause_of()
    except Exception as error:  # noqa: BLE001 - base illisible : aucun geste
        raise HoraireError("pause illisible (%s) : aucun geste" % error) from error
    if not data:
        return None
    from ameesh import packet_pause
    return "%s %s : aucun geste" % (ctx.target, packet_pause.describe(data, ctx.zone))


def run(ctx: Context) -> str:
    """Un tic complet : pause d'abord (#4361 : ni migration ni composition
    pendant un staging hors créneau), puis fraîcheur, schéma et gestes."""
    held = paused(ctx)
    if held:
        return held
    check_fresh(ctx)
    align_schema(ctx)
    return tick(ctx)


def tick(ctx: Context) -> str:
    rows = open_lots(ctx)
    carriers = [row for row in rows if is_carrier(row, ctx.project, ctx.target)]
    if carriers:
        own = [row for row in carriers if is_hourly(row)
               and "paquet-horaire" in str(row.get("source") or "")]
        # #3256 C : une paire parallèle (premier + second) se suit sur place ;
        # le second ne fusionne qu'après le premier (packet_automerge).
        limit = int(getattr(ctx.parallel, "max_paquets", 1) or 1) \
            if ctx.parallel is not None else 1
        if len(own) == len(carriers) == 2 and limit >= 2:
            items = [show(ctx, int(row["id"])) for row in own]
            pair = all(carrier_group(item) or second_peer(item) is not None
                       for item in items)
            if pair:
                items.sort(key=lambda item: (float(item.get("created_ts") or 0),
                                             int(item["id"])))
                return "\n".join(follow(ctx, item) for item in items)
        if len(own) == 1 and len(carriers) == 1:
            return follow(ctx, show(ctx, int(own[0]["id"])))
        return "%s réservée par %s : rien à faire" % (
            ctx.target, ", ".join("#%d" % int(row["id"]) for row in carriers))
    return compose(ctx, recent_lots(ctx))


def session_epoch(ctx: Context) -> int:
    """#4237 : l'époque du bail de la session qui lance une recomposition.

    `AMEESH_LEASE_EPOCH` sous `ameesh role run` ; sinon la garde `packet` de
    `pilot_gesture`, lue par le Python du projet : une session de pilote liée
    par son runner rend son époque, une session qui ne détient plus le bail
    est refusée ici, avant toute marque. Un humain ou le bureau (aucune garde)
    rend 0 : la marque n'a pas d'époque et le délai de composition s'applique.
    """
    if ctx.epoch:
        return ctx.epoch
    code = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from ameesh import config, db, pilot_gesture\n"
        "conn = db.connect(config.load())\n"
        "try:\n"
        "    gate = pilot_gesture.gate(None, conn, 'packet', sys.argv[1])\n"
        "finally:\n"
        "    if conn is not None:\n"
        "        conn.close()\n"
        "print(gate.epoch if gate is not None else 0)\n")
    result = ctx.shell.run([ctx.python, "-c", code, "paquet-horaire recompose",
                            str(ctx.repo / "src")],
                           cwd=ctx.repo, timeout=60)
    if result.returncode != 0:
        raise HoraireError("recomposition refusée, bail non vérifié : %s"
                           % auto._tail(result.text, 5))
    try:
        return max(int(result.stdout.strip().splitlines()[-1]), 0)
    except (ValueError, IndexError):
        raise HoraireError("époque illisible : %r" % result.stdout[-200:]) from None


def pilot_recompose(ctx: Context, carrier: int, exclude: list[int],
                    reasons: list[str]) -> str:
    """#4237 : la recomposition du pilote, marquée comme celles du tic."""
    if reasons and len(reasons) != len(exclude):
        raise InputError("--exclude-reason : %d motif(s) pour %d --exclude"
                         % (len(reasons), len(exclude)))
    item = show(ctx, carrier)
    if not (item.get("state") in OPEN_STATES
            and is_carrier(item, ctx.project, ctx.target) and is_hourly(item)
            and "paquet-horaire" in str(item.get("source") or "")):
        raise InputError("#%d n'est pas un porteur horaire ouvert sur %s : aucun tic "
                         "ne reprendrait sa recomposition" % (carrier, ctx.target))
    sha = frozen_sha(item)
    left = pending(ctx, item, sha)
    if left is not None and running(ctx, left):
        raise InputError("#%d : %s en cours (%s:%d), attendre sa fin"
                         % (carrier, left.step, left.host, left.pid))
    # #4501 : une ci-full tourne sur la tête poussée ; la recomposition
    # l'écraserait au lieu d'attendre son verdict.
    run = ci_in_progress(ctx, item)
    if run is not None:
        raise InputError(
            "#%d : ci-full en cours sur %s (run %s), recomposition refusée"
            % (carrier, sha[:12], run.get("run_id") or "?"))
    ctx.epoch = session_epoch(ctx)
    name = str(item.get("branch") or "")[len(auto.PACKAGE_PREFIX):]
    ctx.say("#%d : recomposition du pilote sans %s" % (
        carrier, ", ".join("#%d" % lot for lot in exclude) or "aucun lot"))
    return attempt(ctx, carrier, name, Attempt(RECOMPOSITION, 1, sha, run_id=PILOT_RUN,
                                               exclude=list(exclude),
                                               only=carrier_group(item),
                                               reasons=list(reasons)))


def pose_git_identity(pilot: str) -> dict:
    """#3916 : la minuterie compose sous l'identité de son rôle pilote.

    `ameesh role run` ne pose aucune identité git et le clone de la minuterie
    n'a pas de `user.*` (à juste titre) : `git merge` échouait « Committer
    identity unknown ». Comme le runner (#834), l'identité passe par
    l'**environnement** (auteur et committer), hérité par paquet-auto et ses
    `git`, jamais par `git config`.
    """
    from ameesh import identity
    env = identity.git_identity(pilot)
    os.environ.update(env)
    return env


DURATION_RE = re.compile(r"^(\d+)\s*(h|m|min)$")
CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_until(text: str, now: float, zone: ZoneInfo) -> float:
    """#4361 : l'échéance d'une pause : durée (`90m`, `2h`), heure locale du
    canon (`20:15`, prochaine occurrence) ou date ISO (sans fuseau : celui du
    canon)."""
    text = (text or "").strip()
    match = DURATION_RE.match(text)
    if match:
        n = int(match.group(1))
        return now + n * (3600 if match.group(2) == "h" else 60)
    match = CLOCK_RE.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise InputError("heure invalide : %s" % text)
        base = datetime.fromtimestamp(now, zone).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        when = base.timestamp()
        if when <= now:
            when = (base + timedelta(days=1)).timestamp()
        return when
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise InputError("échéance illisible : %r (90m, 2h, 20:15 ou date ISO)" % text) from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=zone)
    return when.timestamp()


def _connect():
    from ameesh import config as config_mod, db as db_mod
    return db_mod.connect(config_mod.load())


def read_pause(project: str, target: str) -> dict | None:
    """#4361 : la pause active de la cible, une lecture par tic."""
    from ameesh import packet_pause
    db = _connect()
    try:
        return packet_pause.active(db, project=project, target=target)
    finally:
        close = getattr(db, "close", None)
        if close:
            close()


def _zone() -> tuple[ZoneInfo, object]:
    from ameesh import config as config_mod, mechanics
    rules = mechanics.configured(config_mod.load())
    return ZoneInfo(rules.timezone), rules


def tell(shell: Shell, pilot: str, body: str) -> None:
    """La pause et sa levée sont dites au pilote et au bureau (#4361)."""
    for to in dict.fromkeys((pilot, BUREAU)):
        sent = shell.run(["agent-mail", "send", to, "-"], input_text=body, timeout=120)
        if sent.returncode != 0:
            print("courrier à %s non envoyé : %s" % (to, auto._tail(sent.text, 5)),
                  file=sys.stderr)


def main_pause(args, *, connect=_connect, shell: Shell | None = None,
               now: float | None = None) -> int:
    """`pause`, `resume`, `status` (#4361) : l'état vit au journal partagé."""
    from ameesh import packet_pause
    try:
        zone, _ = _zone()
    except Exception as error:  # noqa: BLE001 - sans canon, aucune heure sûre
        print("paquet-horaire : fuseau du canon compilé illisible (%s)" % error,
              file=sys.stderr)
        return 1
    now = time.time() if now is None else now
    by = args.by or os.environ.get("AGENT_MAIL_NAME") or os.environ.get("AMEESH_ROLE") \
        or os.environ.get("USER") or "inconnu"
    try:
        db = connect()
    except Exception as error:  # noqa: BLE001 - base injoignable
        print("paquet-horaire : base injoignable (%s)" % error, file=sys.stderr)
        return 1
    where = "%s/%s" % (args.project, args.target)
    try:
        if args.command == "status":
            data = packet_pause.active(db, project=args.project, target=args.target, now=now)
            print("%s : %s" % (where, packet_pause.describe(data, zone) if data
                               else "aucune pause, le tic compose"))
            return 0
        if args.command == "resume":
            before = packet_pause.active(db, project=args.project, target=args.target, now=now)
            packet_pause.resume(db, project=args.project, target=args.target, by=by)
            text = "paquet horaire %s : pause levée par %s%s" % (
                where, by, "" if before else " (aucune pause active)")
        else:
            until = parse_until(args.until, now, zone)
            data = packet_pause.pause(db, project=args.project, target=args.target,
                                      until=until, reason=args.reason, by=by, now=now)
            text = "paquet horaire %s : %s ; le tic reprend seul à l'échéance " \
                   "(`paquet-horaire.py resume` pour lever avant)" % (
                       where, packet_pause.describe(data, zone))
    except InputError as error:
        print("paquet-horaire : %s" % error, file=sys.stderr)
        return 2
    except packet_pause.PauseError as error:
        print("paquet-horaire : pause refusée : %s" % error, file=sys.stderr)
        return 2
    print(text, flush=True)
    tell(shell or Shell(), args.pilot, text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Paquet horaire déclenché par minuterie, sans agent (#3845).")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("tick", help="un tic : composer, publier ou suivre")
    again = sub.add_parser(
        "recompose", help="#4237 : recomposition du pilote, marquée « tentative » "
                          "et reprise par le tic du détenteur courant")
    again.add_argument("--carrier", type=int, required=True, help="porteur horaire")
    again.add_argument("--exclude", type=int, action="append", default=[],
                       metavar="LOT", help="lot retiré (répétable)")
    again.add_argument("--exclude-reason", action="append", default=[], metavar="TEXTE",
                       help="motif au manifeste, apparié par ordre aux --exclude")
    for each in (one, again):
        each.add_argument("--repo", default=".", help="clone du projet (défaut : .)")
        each.add_argument("--actor", default=None,
                          help="auteur des gestes (défaut : AMEESH_ROLE, sinon --pilot)")
        each.add_argument("--python", default=None,
                          help="interpréteur des scripts enfants (défaut : celui-ci)")
        each.add_argument("--dry-run", action="store_true",
                          help="lire et dire, sans créer, pousser, noter ni écrire")
    hold = sub.add_parser("pause", help="#4361 : suspendre le tic jusqu'à une échéance")
    hold.add_argument("--until", required=True,
                      help="échéance obligatoire : 90m, 2h, 20:15 (canon) ou date ISO")
    hold.add_argument("--reason", required=True, help="raison, dite au tic et au pilote")
    lift = sub.add_parser("resume", help="#4361 : lever la pause avant l'échéance")
    look = sub.add_parser("status", help="#4361 : la pause active, s'il y en a une")
    for each in (one, again, hold, lift, look):
        each.add_argument("--target", default="main")
        each.add_argument("--project", default="ameesh")
        each.add_argument("--pilot", default="mesh-design",
                          help="destinataire des courriers et assigné du porteur")
    for each in (hold, lift, look):
        each.add_argument("--by", default=None,
                          help="auteur (défaut : AGENT_MAIL_NAME, AMEESH_ROLE, USER)")
    args = parser.parse_args(argv)
    if args.command in ("pause", "resume", "status"):
        return main_pause(args)
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print("paquet-horaire : --repo %s n'est pas un clone" % repo, file=sys.stderr)
        return 2
    actor = args.actor or os.environ.get("AMEESH_ROLE") or args.pilot
    pose_git_identity(args.pilot)
    try:
        zone, document = _zone()
        unit_tests = getattr(document, "packet_tests", None)
        parallel = getattr(document, "paquet", None)
    except Exception as error:  # noqa: BLE001 - sans canon, aucune heure sûre
        print("paquet-horaire : fuseau du canon compilé illisible (%s)" % error,
              file=sys.stderr)
        return 1
    ctx = Context(repo=repo, target=args.target, project=args.project,
                  pilot=args.pilot, actor=actor, shell=Shell(),
                  python=args.python or auto.project_python(repo),
                  dry_run=args.dry_run, zone=zone, epoch=lease_epoch(),
                  unit_tests=unit_tests, parallel=parallel,
                  pause_of=lambda: read_pause(args.project, args.target))
    try:
        if args.command == "recompose":
            # geste explicite du pilote (#4237) : la pause (#4361) ne vise que le tic
            check_fresh(ctx)
            align_schema(ctx)
            print(pilot_recompose(ctx, args.carrier, args.exclude, args.exclude_reason),
                  flush=True)
        else:
            print(run(ctx), flush=True)
        return 0
    except InputError as error:
        print("paquet-horaire : %s" % error, file=sys.stderr)
        return 2
    except (HoraireError, auto.PaquetAutoError) as error:
        print("paquet-horaire : %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
