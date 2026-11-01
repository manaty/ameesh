# SPDX-License-Identifier: AGPL-3.0-only
"""#2813 : la fusion automatique d'un paquet, armée à sa publication (canon §12).

Décision owner du 20/10 (« yes » au bureau, fil du lot #2813) : aucun geste
ameesh n'armait la fusion des paquets ; h-1700, h-1900 et h-2000 sont restés
verts et ouverts jusqu'à la fusion manuelle du bureau. Le geste qui publie le
paquet arme désormais sa fusion sur la tête gelée ; tout nouveau push la
désarme, et la publication suivante la réarme sur la nouvelle tête.

**Pourquoi pas l'auto-fusion GitHub** (`gh pr merge --auto`, relecture
ameesh-cx2 de 249b5911, B1/P1) : GitHub ne vérifie `--match-head-commit`
qu'à l'armement et ne désarme pas sur un push d'un compte en écriture (#1656,
`fire` P1-4). Une tête poussée après la publication et déjà verte (commit
déjà testé, run lancé à la main) fusionnerait avant toute surveillance. La
fusion est donc faite **par ameesh**, et aucune auto-fusion GitHub n'est
laissée armée. Le geste lie la tête **et** la cible (R2, ameesh-cx2) :
`gh pr merge --match-head-commit` ne lie que la tête, une PR reciblée juste
avant fusionnerait ailleurs ; l'API « merge a branch » (`POST
repos/<slug>/merges`, `base=<cible épinglée>`, `head=<tête publiée>`)
écrit la branche qu'elle nomme et le commit qu'elle nomme (`merge_into`).

* **Publier** un porteur `integration/**`, c'est demander sa suite complète :
  `ameesh ci run <porteur> --full` (étiquette `ci-full-<tête>`). Juste après la
  pose (ou sur une composition déjà demandée ou verte), `arm` épingle au
  journal du lot la tête, la PR et la cible publiées (`auto-merge armed …
  base=<cible>`), désarme une auto-fusion GitHub trouvée sur la PR, puis
  tente la fusion tout de suite (composition déjà verte).
* **Seule une garde de la forge est prouvée** : l'armement exige un check
  requis imposé par GitHub sur la cible (protection classique ou ruleset,
  `fire.required_checks` sans déclaration de projet), et la fusion exige ces
  checks présents et `SUCCESS` sur la tête exacte (`fire.ready_pr`) — un
  jeton administrateur passerait outre la protection, ameesh la relit ; une
  déclaration de projet s'y ajoute, elle ne remplace jamais la forge.
  Sans check imposé (manaty/nexlink, privé sans GitHub Pro, protection en
  403), rien n'est armé : l'équivalent est le feu du pilote à la composition
  (`ameesh work fire <porteur> --sha <tête>`), que le message rappelle.
* **Le passage** (`watch`, `ameesh release tick` des projets de
  `packet_watch`, toutes les cinq minutes) relit chaque porteur ouvert armé :
  tête, cible ou gel changé depuis la publication → désarmé (journal, et
  auto-fusion GitHub éventuelle retirée). Une cible qui n'a avancé que par
  des atterrissages de coupure (ligne de version seule et notes
  `docs/versions/*.md`, manaty/home#173) ne désarme pas : le contenu testé
  n'a pas changé. Sinon la fusion passe les mêmes
  portes que le feu (R1, ameesh-cx3) : `fire.ready_verdicts` (classe, avis
  requis et indépendants sur la tête gelée, membres d'un porteur et delta,
  #2766), puis au moins un check imposé par la forge — et tous ceux épinglés
  à la publication — `SUCCESS` sur la tête exacte, avec le projet résolu
  (checks déclarés, `required_statuses`, `ci_required` ; R2, ameesh-cx3) par
  `fire.ready_pr`. La PR est relue juste avant le geste (tête, cible, état).
  La vitesse du passage ne fait que la latence de la fusion, jamais sa
  sûreté : une autre tête ou une autre cible ne fusionne jamais par ce
  chemin.

La trace est le journal du lot (aucun état local) : `auto-merge armed …`,
`disarmed …`, `merged …`, `not-armed …` et `held …`, avec dépôt, PR, tête et
cible. #3175 : un refus du passage (`pending`, `error`) n'est jamais muet ;
`held …` en porte le motif, une fois par motif et par armement (le passage
suivant qui refuse pour la même raison n'écrit rien). La
publication et le passage d'un même porteur se sérialisent sous le bail
`auto-merge:<lot>` ; le passage relit les marques sous ce bail.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
from urllib.parse import quote, unquote

from . import fire, land, packet_parallel, storage, work

#: une tête de porteur de paquet (§12) ; `integration/paquet-h-…`, `…-nexlink`
CARRIER_PREFIX = "integration/"
#: décision owner : un commit de fusion, comme les fusions manuelles des paquets
METHOD = "merge"
#: #3256 C : la note du composeur qui fait d'un porteur le second d'une paire
#: parallèle ; le premier est nommé par son numéro de lot.
PARALLEL_SECOND = re.compile(
    r"^paquet-horaire: second paquet parallèle de #(\d+)\b")
#: la preuve (une fois par tête et par cible) que le second peut fusionner
PARALLEL_PROOF = "parallel-second proof "
PARALLEL_CONFLICT = "parallel-second conflict "
#: le délai des contrôles de gel sur l'arbre fusionné (#3256 C) ; borné sous le
#: bail `auto-merge` (300 s) pour qu'un passage ne le laisse pas expirer.
PARALLEL_FREEZE_TIMEOUT = 120.0
#: états d'un porteur que `watch` relit
OPEN_STATES = (
    lifecycle.REQUESTED,
    lifecycle.IN_PROGRESS,
    lifecycle.QA,
    lifecycle.VALIDATED,
    lifecycle.BLOCKED,
    lifecycle.WAITING_HUMAN,
)
#: durée du bail `auto-merge:<lot>` qui sérialise la publication et le passage ;
#: il est renouvelé avant chaque geste (B4 : plusieurs appels `gh` de 60 à 120 s)
LOCK_TTL_S = 300.0
#: L165 (EP4) : marge du bail élu avant `merge_into` (POST /merges, 120 s de
#: timeout + lecture) ; la course résiduelle est documentée dans
#: docs/HOST-HANDOFFS.md.
AUTOMERGE_CALL_MARGIN_S = 180.0
#: une marque du journal du lot ; un armement porte toujours sa cible
MARK_RE = re.compile(
    r"^auto-merge (?P<state>armed|disarmed|merged|not-armed|held) "
    r"repo=(?P<repo>[\w.-]+/[\w.-]+) pr=(?P<pr>\d+) sha=(?P<sha>[0-9a-f]{40})"
    r"(?: base=(?P<base>[\w./-]+))?(?: method=[a-z]+)?(?: required=(?P<required>\S+))?"
    r"(?: composition=(?P<composition>[0-9a-f]{40}))?"
    r"(?:\s|$)(?: *— *(?P<why>.*))?", re.S)
#: marques qui referment un armement de la même PR
CLOSING = ("disarmed", "merged")
_SHA = re.compile(r"[0-9a-f]{40}")
#: #3534 / manaty/home#173 : un atterrissage de coupure ne change que la
#: ligne de version et les notes de version.
NOTES_DIR = "docs/versions"
_VERSION_LINE = re.compile(r'^version\s*=\s*"[^"\s]+"\s*$')
#: #3558 : les agrégats requis dont le rouge se prouve par les parts lues —
#: les mêmes noms que `release_cutoff._aggregates` (#3244). Un agrégat n'a
#: pas de test à son nom : il est le seul check requis exempté de la preuve
#: par part. Tout autre check requis rouge (`lint`, une part, un portier)
#: exige sa propre part lue et son flux `FAILURE` (relectures B/P1 du 25/10).
INHERITED_RED_AGGREGATES = frozenset((
    "tests", "tests (dispatch)", "suite complète du paquet / tests"))
#: #3558 (ameesh-dev4 + ameesh-cx3 B/P1) : ligne que `ci-flow.py` ajoute à la
#: sortie d'un flux quand `MAX_TEXT` l'a obligé à omettre des échecs. Ces
#: événements ne sont pas publiés : `ci.events_of` l'ignore, donc une telle
#: sortie ne prouve jamais la liste complète des échecs.
OMITTED_EVENTS_RE = re.compile(
    r"(?m)^\d+ échec\(s\) de plus, hors de la limite de taille de GitHub\.\s*$")


def carrier(head_ref) -> bool:
    """La source d'une PR est-elle un porteur de paquet ?"""
    return str(head_ref or "").strip().startswith(CARRIER_PREFIX)


def gh_of(runner):
    """Un `gh` appelable (`gh(args, timeout=…)` → CompletedProcess, celui de
    `land` et `fire`) à partir d'un `plan_github.Gh` (`run(argv)` → tuple)."""
    def call(args, timeout: float = 60.0):
        code, out, err = runner.run(list(args))
        return subprocess.CompletedProcess(["gh", *args], code, out or "", err or "")
    return call


# --------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------

def marks(db, lot: int) -> list[dict]:
    """Les marques de fusion automatique du lot, de la plus ancienne à la plus
    récente.

    La lecture porte sur les seules notes `auto-merge …` (#3279) : les notes
    `held` d'un porteur armé pouvaient remplir la fenêtre de 200 événements de
    `work.events` et en chasser la marque d'armement, faisant croire que rien
    n'était armé. Une marque de contrôle ne dépend jamais du nombre de notes
    qui la suivent."""
    out = []
    for ev in work.events_noted(db, int(lot), "auto-merge ", 0.0) or ():
        m = MARK_RE.match((ev.get("note") or "").strip())
        if m:
            out.append({"id": int(ev.get("id") or 0), "state": m.group("state"),
                        "repo": m.group("repo"), "pr": int(m.group("pr")),
                        "sha": m.group("sha"), "base": m.group("base") or "",
                        "composition": (m.group("composition") or "").strip().lower(),
                        "required": tuple(unquote(name) for name in
                                          (m.group("required") or "").split(",") if name),
                        "why": (m.group("why") or "").strip()})
    return sorted(out, key=lambda m: m["id"])


def active(rows) -> dict | None:
    """Le dernier armement qu'aucun désarmement ni fusion n'a suivi, ou None.

    Un armement sans cible ni checks requis ne compte pas (la cible et les
    checks imposés à la publication sont épinglés, relectures R0/R1). Un
    `not-armed` ne l'efface pas : une republication refusée laisse l'armement
    précédent sous la garde du passage."""
    current = None
    for row in rows or ():
        if row["state"] == "armed":
            current = row if row["base"] and row["required"] else None
        elif row["state"] in CLOSING and current is not None \
                and current["pr"] == row["pr"] and current["repo"] == row["repo"]:
            current = None
    return current


def publication(db, lot: int, sha: str) -> dict:
    """#3611 : ce que le journal du porteur dit de sa publication sur `sha`.

    `requested` : une demande `ci-full-<sha>` posée par `ameesh ci run --full`
    (marque `ci-label applied`) ; `armed` : la fusion automatique armée sur
    cette tête (`active`), avec sa PR et sa cible. Lecture locale seulement :
    la lecture porte sur les seules notes `ci-label `, comme `marks` (#3279)."""
    from . import ci
    sha = str(sha or "").strip().lower()
    requested = False
    for ev in work.events_noted(db, int(lot), "ci-label ", 0.0) or ():
        m = ci.REQUEST_RE.match((ev.get("note") or "").strip())
        if m and m.group("state") == "applied" and m.group("mode") == "full" \
                and fire._same_head(m.group("sha"), sha):
            requested = True
    current = active(marks(db, lot))
    armed = bool(current and fire._same_head(current["sha"], sha))
    return {"requested": requested, "armed": armed,
            "pr": current["pr"] if armed else None,
            "base": current["base"] if armed else ""}


def _note(db, lot: int, state: str, slug: str, number: int, sha: str, rest: str,
          actor: str) -> None:
    work.note(db, int(lot), "auto-merge %s repo=%s pr=%d sha=%s %s"
              % (state, slug, int(number), sha, rest.strip()), actor=actor)


def _fenced_hold(db, fence, *, margin_s: float = 0.0) -> bool:
    """L165 (EP4) : renouvelle la garde si elle existe ; False si perdue."""
    if fence is None:
        return True
    from . import pilot_gesture
    try:
        fence.hold(db, margin_s=margin_s)
    except pilot_gesture.GestureRefused:
        return False
    return True


def _note_fenced(db, fence, lot: int, state: str, slug: str, number: int, sha: str,
                 rest: str, actor: str, *, margin_s: float = 0.0) -> bool:
    """Écrit la marque dans la même transaction que le hold du bail élu.

    Sans garde (exécuteur/humain), c'est la note simple. Avec une garde, une
    époque périmée n'écrit pas la marque : c'est la correction B1 de la
    relecture cx2 (le marqueur `armed` ne doit pas survivre à une bascule).
    """
    if fence is None:
        _note(db, lot, state, slug, number, sha, rest, actor)
        return True
    from . import pilot_gesture
    with db.transaction() as tx:
        try:
            fence.hold(tx, margin_s=margin_s)
        except pilot_gesture.GestureRefused:
            return False
        _note(tx, lot, state, slug, number, sha, rest, actor)
        return True


def _lock(db, lot: int):
    """Le bail `auto-merge:<lot>`, ou None si un autre le tient.

    La publication et le passage le prennent tous deux : sans lui, un passage
    qui a lu l'ancien armement désarmerait celui que la publication vient de
    poser sur la nouvelle tête."""
    ops = storage.of(db).work
    owner = "auto-merge:%s:%d" % (socket.gethostname(), os.getpid())
    key = "auto-merge:%d" % int(lot)
    epoch = ops.land_lock(key, owner, LOCK_TTL_S)
    return (ops, owner, epoch, key) if epoch is not None else None


def _renew(handle) -> bool:
    """Le bail est-il encore le nôtre ? Le renouvelle au passage. Appelé juste
    avant chaque geste (désarmement, fusion, marque) : un détenteur périmé
    n'agit pas (même règle que `fire._renew`)."""
    if handle is None:          # dry-run, ou appel de test sans bail
        return True
    ops, owner, epoch, key = handle
    try:
        return ops.land_lock(key, owner, LOCK_TTL_S, epoch=epoch) is not None
    except Exception:  # noqa: BLE001 - bail illisible : on ne mute pas
        return False


def _unlock(handle) -> None:
    ops, owner, epoch, key = handle
    try:
        ops.land_unlock(key, owner, epoch)
    except Exception:  # noqa: BLE001 - le bail expire seul
        pass


# --------------------------------------------------------------------------
# cible et base de composition (#3534, manaty/home#173)
# --------------------------------------------------------------------------

def _git(repo: str, *args: str, timeout: float = 60.0) -> str | None:
    """La sortie d'un git local, ou None (dépôt absent, commande en échec)."""
    try:
        proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _composition_base(db, lot: int, sha: str, target: str = "") -> str:
    """La base de composition enregistrée du paquet (`work_packet_members`),
    pour la tête `sha` ; "" si absente ou illisible.

    #3534 : c'est la seule borne qui ne dépende pas du SHA courant de la
    cible ; elle est écrite par `work packet record` au moment de la
    composition."""
    if db is None:
        return ""
    try:
        rows = storage.of(db).work.packet_members(package_ids=[int(lot)])
    except Exception:  # noqa: BLE001 - base ou schéma illisible : repli
        return ""
    head = str(sha or "").strip().lower()
    for row in rows or ():
        row = row or {}
        if str(row.get("head_sha") or "").strip().lower() != head:
            continue
        base = str(row.get("base_sha") or "").strip().lower()
        if not base:
            continue
        recorded_target = str(row.get("package_target") or "").strip()
        if target and recorded_target and recorded_target != target:
            continue
        return base
    return ""


def _cutoff_commit(repo: str, sha: str) -> bool:
    """Un commit d'atterrissage de coupure (manaty/home#173) : message de
    release, et diff contre sa première parente limité à la ligne de version
    de `pyproject.toml` et aux notes `docs/versions/*.md`."""
    message = _git(repo, "log", "-1", "--format=%B", sha) or ""
    if "ameesh-release:" not in message:
        return False
    parents = (_git(repo, "rev-list", "--parents", "-n", "1", sha) or "").split()
    if len(parents) < 2:
        return False
    parent = parents[1]
    names = (_git(repo, "diff", "--name-only", "--no-renames", parent, sha)
             or "").splitlines()
    if not names:
        return False
    for name in names:
        if name == "pyproject.toml":
            patch = _git(repo, "diff", "-U0", parent, sha, "--", "pyproject.toml")
            if patch is None:
                return False
            removed, added = [], []
            for line in patch.splitlines():
                if line.startswith(("+++", "---")):
                    continue
                if line.startswith("+"):
                    added.append(line[1:])
                elif line.startswith("-"):
                    removed.append(line[1:])
            if len(removed) != 1 or len(added) != 1:
                return False
            if not (_VERSION_LINE.match(removed[0].strip())
                    and _VERSION_LINE.match(added[0].strip())):
                return False
        elif name.startswith(NOTES_DIR + "/") and name.endswith(".md"):
            continue
        else:
            return False
    return True


def _cutoff_landing_only(repo: str, base: str, head: str) -> bool:
    """La cible n'a avancé que par des atterrissages de coupure (#173).

    `base` doit être un ancêtre de `head` ; chaque commit de l'intervalle doit
    être un atterrissage (version + notes). Un dépôt illisible, une cible
    absente ou un commit de code rendent False : on ne fusionne pas un arbre
    que la CI n'a pas testé."""
    if not repo or not base or not head or base == head:
        return False
    if _git(repo, "merge-base", "--is-ancestor", base, head) is None:
        return False
    commits = _git(repo, "rev-list", "--reverse", "%s..%s" % (base, head))
    if commits is None:
        return False
    shas = commits.split()
    return bool(shas) and all(_cutoff_commit(repo, sha) for sha in shas)


def _merge_base(repo: str, a: str, b: str) -> str:
    """Le dernier commit commun de `a` et `b`, ou ""."""
    if not repo or not a or not b:
        return ""
    return (_git(repo, "merge-base", a, b) or "").strip().lower()


def _target_tested(repo: str, target: str, head: str, composition: str = "") -> bool:
    """La cible est-elle couverte par la tête testée `head` ?

    #3534 (B2) : la borne n'est pas la base du manifeste mais le dernier
    commit de la cible **contenu dans la tête** (`merge-base`) ; c'est lui qui
    a été testé avec le paquet. Si la cible a ensuite avancé, seul le delta
    depuis ce commit doit être un atterrissage de coupure. Sans tête lisible
    (clone en retard, tête absente), le repli est la base du manifeste : une
    cible qui n'a pas bougé depuis la composition reste couverte."""
    if not repo or not target or not head:
        return False
    included = _merge_base(repo, target, head)
    if not included:
        return bool(composition) and _cutoff_landing_only(repo, composition, target)
    if included == target:
        return True
    return _cutoff_landing_only(repo, included, target)


# --------------------------------------------------------------------------
# #3256 C : le second paquet d'une paire parallèle fusionne après le premier
# --------------------------------------------------------------------------

def _parallel_peer(db, lot: int) -> int | None:
    """Le porteur du premier paquet, si `lot` est le second d'une paire.

    La note est celle du composeur (`paquet-horaire: second paquet parallèle
    de #<premier> groupe=…`), écrite avant la publication de la CI. Aucune
    autre source ne fait un second paquet : un porteur ordinaire reste
    désarmé quand la cible avance.
    """
    try:
        events = work.events_noted(db, int(lot), "paquet-horaire: second paquet ",
                                   0.0)
    except Exception:  # noqa: BLE001 - un journal illisible ne suppose rien
        return None
    for event in reversed(events or ()):
        match = PARALLEL_SECOND.match(str(event.get("note") or "").strip())
        if match:
            return int(match.group(1))
    return None


def _first_head(db, peer: int) -> str:
    """La tête gelée du premier porteur (la tête qu'il a fusionnée)."""
    try:
        row, marks = fire._fresh(db, int(peer))
    except Exception:  # noqa: BLE001 - lecture du premier impossible
        return ""
    if row is None:
        return ""
    freeze = fire.freeze_of(marks) or {}
    return str(freeze.get("sha") or "").strip().lower()


def _first_state(db, peer: int) -> str:
    """L'état courant du premier porteur, ou "" si illisible."""
    try:
        row, _marks = fire._fresh(db, int(peer))
    except Exception:  # noqa: BLE001 - un état illisible ne suppose rien
        return ""
    return str((row or {}).get("state") or "")


def _first_merge(repo: str, composition: str, target: str,
                 first_head: str) -> str:
    """Le commit de fusion du premier paquet dans la cible, ou "".

    Seule une avance `composition..target` composée du commit de fusion du
    premier paquet (second parent = sa tête gelée) et d'atterrissages de
    coupure est acceptée : un autre commit de code rend "" et le second est
    désarmé comme un paquet ordinaire.
    """
    if not repo or not composition or not target or not first_head:
        return ""
    if _git(repo, "merge-base", "--is-ancestor", first_head, target) is None:
        return ""
    commits = _git(repo, "rev-list", "--first-parent",
                   "%s..%s" % (composition, target))
    if commits is None:
        return ""
    merged = ""
    for commit in commits.split():
        parents = (_git(repo, "rev-list", "--parents", "-n", "1", commit)
                   or "").split()
        if len(parents) >= 3 and parents[2] == first_head:
            if merged:
                return ""       # deux fusions de la même tête : ambigu
            merged = commit
            continue
        if not _cutoff_commit(repo, commit):
            return ""
    return merged


def _parallel_freeze_checks(repo: str, target: str, head: str,
                            timeout: float = PARALLEL_FREEZE_TIMEOUT) -> tuple:
    """Les contrôles de gel (#2542) sur l'arbre `target + head`, fusion propre.

    Le second n'est pas rebasé par un force-push (sa tête testée resterait
    celle de la CI) : la fusion de sa tête dans la cible avancée est essayée
    dans un worktree jetable, puis `scripts/freeze-checks.py` y tourne.

    Rend `(état, motif)` : `ok`, `red` (la fusion ou un contrôle échoue — le
    second revient en file) ou `inconclusive` (worktree, délai, OSError : la
    preuve manque, §3).
    """
    if not repo:
        return "inconclusive", "no clone to run the freeze checks"
    try:
        base = tempfile.mkdtemp(prefix="ameesh-parallel-",
                                dir=os.environ.get("TMPDIR") or None)
        os.rmdir(base)
    except OSError as exc:
        return "inconclusive", "worktree could not be prepared: %s" % exc
    added = _git(repo, "worktree", "add", "--detach", base, target)
    if added is None:
        return "inconclusive", "worktree could not be created"
    try:
        try:
            merge = subprocess.run(
                ["git", "-C", base, "merge", "--no-commit", "--no-ff", head],
                capture_output=True, text=True, timeout=120.0)
        except (OSError, subprocess.SubprocessError) as exc:
            return "inconclusive", "merge could not run: %s" % exc
        if merge.returncode != 0:
            return "red", ("merge-tree conflict: %s"
                           % (merge.stderr or merge.stdout or "").strip()[:300])
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.join(base, "src")] + ([env["PYTHONPATH"]]
                                           if env.get("PYTHONPATH") else []))
        try:
            from pathlib import Path
            from .project_python import project_python
            checks = subprocess.run(
                [project_python(Path(base)), os.path.join(base, "scripts", "freeze-checks.py")],
                cwd=base, capture_output=True, text=True, timeout=timeout, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            return "inconclusive", "freeze checks could not run: %s" % exc
        if checks.returncode != 0:
            tail = (checks.stdout or "") + (checks.stderr or "")
            return "red", ("freeze-checks rc %d: %s"
                           % (checks.returncode, " ".join(tail.split())[-500:]))
        return "ok", ""
    finally:
        _git(repo, "worktree", "remove", "--force", base)
        shutil.rmtree(base, ignore_errors=True)


def _carrier_branch(db, lot: int) -> str:
    """La branche portée par un lot (`integration/paquet-…`), ou ""."""
    try:
        row, _marks = fire._fresh(db, int(lot))
    except Exception:  # noqa: BLE001 - une branche illisible ne prouve rien
        return ""
    return str((row or {}).get("branch") or "").strip()


def _carrier_manifest(repo: str, head: str, branch: str) -> str | None:
    """Le manifeste §12 tel qu'il est **figé** dans `head`, ou None."""
    if not repo or not head or not branch.startswith(CARRIER_PREFIX):
        return None
    # `integration/paquet-<nom>` → `reviews/integration-paquet-<nom>.md`
    # (même dérivation que `paquet-auto.manifest_rel`).
    rel = "reviews/%s.md" % branch.replace("/", "-", 1)
    return _git(repo, "show", "%s:%s" % (head, rel))


def _parallel_manifests_disjoint(db, repo: str, lot: int, peer: int,
                                 first_head: str, second_head: str) -> tuple:
    """Les deux manifestes figés prouvent-ils encore la disjonction ?

    #3256 C (B cx1) : la partition lit l'inventaire au moment du partage,
    # mais `--only` ne transmet que des identifiants et `compose_packet` relit
    # les gels courants. Un regel entre le partage et la composition (ou une
    # reprise/recomposition) peut donc donner aux deux porteurs un fichier
    # commun, alors que la note de paire et la preuve de fusion ne regardaient
    # que la propreté du merge-tree. Ici on relit les **manifestes des deux
    # têtes testées** : chaque lot y porte le commit réellement composé, et
    # l'on recalcule ses fichiers depuis la base du manifeste, plus les
    # dépendances courantes. Une dépendance croisée ou un fichier commun est
    # un négatif déterministe (le second revient en file), même si la fusion
    # est propre ; une lecture impossible laisse la preuve manquante.
    """
    first_branch = _carrier_branch(db, peer)
    second_branch = _carrier_branch(db, lot)
    if not first_branch or not second_branch:
        return "inconclusive", "branche de la paire illisible"
    first_text = _carrier_manifest(repo, first_head, first_branch)
    second_text = _carrier_manifest(repo, second_head, second_branch)
    if first_text is None or second_text is None:
        return "inconclusive", "manifeste de la paire illisible dans la tête testée"
    first_units = packet_parallel.manifest_units(first_text)
    second_units = packet_parallel.manifest_units(second_text)
    if not first_units or not second_units:
        return "inconclusive", ("manifeste de la paire sans table d'admissions "
                                "lisible")
    first_lots = [unit["work_item"] for unit in first_units]
    second_lots = [unit["work_item"] for unit in second_units]
    if len(set(first_lots)) != len(first_lots) \
            or len(set(second_lots)) != len(second_lots):
        return "conflict", "manifeste de la paire : un lot y figure deux fois"
    overlap = sorted(set(first_lots) & set(second_lots))
    if overlap:
        return "conflict", ("lot(s) %s présents dans les deux manifestes"
                            % ", ".join("#%d" % item for item in overlap))
    first_base = packet_parallel.manifest_base(first_text)
    second_base = packet_parallel.manifest_base(second_text)
    if not first_base or not second_base:
        return "inconclusive", "base du manifeste de la paire illisible"
    from . import packet_candidates
    files: dict[int, set[str]] = {}
    for units, base in ((first_units, first_base), (second_units, second_base)):
        for unit in units:
            try:
                files[unit["work_item"]] = set(packet_candidates.changed_files(
                    repo, base, unit["source"]))
            except Exception as exc:  # noqa: BLE001 - une lecture impossible ne conclut pas
                return "inconclusive", ("fichiers du lot #%d illisibles: %s"
                                        % (unit["work_item"], exc))
    first_files: set[str] = set()
    second_files: set[str] = set()
    for item in first_lots:
        first_files |= files[item]
    for item in second_lots:
        second_files |= files[item]
    shared = sorted(first_files & second_files)
    if shared:
        return "conflict", ("fichier(s) commun(s) aux deux manifestes testés : %s"
                            % ", ".join(shared))
    from . import work_dependencies
    try:
        edges = work_dependencies.grouped(
            db, sorted(set(first_lots) | set(second_lots)))
    except Exception as exc:  # noqa: BLE001 - une lecture impossible ne conclut pas
        return "inconclusive", "dépendances de la paire illisibles: %s" % exc
    first_set, second_set = set(first_lots), set(second_lots)
    cross = []
    for work_item, rows in (edges or {}).items():
        try:
            work_item = int(work_item)
        except (TypeError, ValueError):
            continue
        for edge in rows or ():
            try:
                blocker = int(edge.get("blocker_id"))
            except (TypeError, ValueError):
                continue
            if (work_item in first_set and blocker in second_set) \
                    or (work_item in second_set and blocker in first_set):
                cross.append((work_item, blocker))
    if cross:
        return "conflict", ("dépendance(s) croisées entre les deux manifestes "
                            "testés : %s" % ", ".join(
                                "#%d→#%d" % pair for pair in cross))
    return "ok", ""


def _parallel_proof(db, repo: str, lot: int, *, composition: str, target: str,
                    sha: str, dry_run: bool) -> dict:
    """Le second peut-il fusionner malgré la cible avancée ?

    Rend `{state: none|wait|conflict|inconclusive|ok, why, merge, marker}`.
    `none` : le porteur n'est pas un second, la garde ordinaire s'applique.
    `wait` : le premier n'a pas encore fusionné. `conflict` : la fusion du
    second dans la cible avancée est un négatif déterministe (conflit,
    manifestes plus disjoints, contrôle de gel rouge) — le second revient en
    file. `inconclusive` : la preuve manque (manifeste, délai, worktree,
    OSError), une recomposition du même porteur est demandée (§3). `ok` : les deux
    manifestes testés restent disjoints, la fusion est propre et les contrôles
    verts ; la marque est écrite par l'appelant sous le bail renouvelé, puis
    la fusion peut suivre sans relancer la suite complète.
    """
    peer = _parallel_peer(db, lot)
    if peer is None:
        return {"state": "none"}
    first_head = _first_head(db, peer)
    if not first_head:
        return {"state": "wait", "why": "premier paquet #%d sans tête gelée lisible"
                                         % peer}
    merge = _first_merge(repo, composition, target, first_head)
    if not merge:
        if _git(repo, "merge-base", "--is-ancestor", first_head, target) is None:
            return {"state": "wait",
                    "why": "second paquet : le premier #%d n'est pas encore fusionné"
                           % peer}
        return {"state": "none"}    # la cible a avancé par un autre commit de code
    if dry_run:
        return {"state": "none"}
    # #3256 C (B cx1) : l'exemption de suite complète n'est due qu'à la
    # disjonction **réellement testée**. Elle se relit sur les deux manifestes
    # figés à chaque passage, reprises comprises — avant toute preuve déjà
    # consignée, qui ne couvre que la propreté de l'arbre fusionné.
    disjoint, why = _parallel_manifests_disjoint(db, repo, lot, peer,
                                                 first_head, sha)
    if disjoint == "conflict":
        return {"state": "conflict", "why": why}
    if disjoint != "ok":
        return {"state": "inconclusive", "why": why}
    # Preuve déjà consignée pour cette tête et cette cible : ne pas rejouer.
    try:
        notes = work.events_noted(db, int(lot), PARALLEL_PROOF, 0.0) or ()
    except Exception:  # noqa: BLE001 - un journal illisible rejoue la preuve
        notes = ()
    marker = "sha=%s target=%s merge=%s" % (sha, target, merge)
    for event in notes:
        if marker in str(event.get("note") or ""):
            return {"state": "ok", "merge": merge, "marker": marker,
                    "why": "preuve déjà consignée"}
    from . import packet_candidates
    try:
        merged = packet_candidates.merge_tree(repo, target, sha)
    except Exception as exc:  # noqa: BLE001 - une lecture impossible ne conclut pas
        return {"state": "inconclusive", "why": "merge-tree illisible: %s" % exc}
    if merged.get("state") == "error":
        return {"state": "inconclusive",
                "why": "merge-tree illisible: %s" % (merged.get("detail") or "?")}
    if merged.get("state") != "clean":
        return {"state": "conflict",
                "why": "fusion du second dans la cible avancée non propre: %s"
                       % (merged.get("detail") or ", ".join(merged.get("files") or ())
                          or merged.get("state"))}
    state, why = _parallel_freeze_checks(repo, target, sha)
    if state == "red":
        return {"state": "conflict", "why": why}
    if state != "ok":
        return {"state": "inconclusive", "why": why}
    return {"state": "ok", "merge": merge, "marker": marker,
            "why": "merge-tree clean, freeze-checks green"}


def _parallel_return_to_queue(db, lot: int, row: dict, why: str, actor: str) -> None:
    """Le second en conflit revient en file : son porteur est clos abandonné.

    Les unités restent en `qa` avec leurs verdicts ; le prochain paquet les
    reprend. La clôture n'est pas fatale au passage : le courrier et la note
    disent le motif même si elle échoue.
    """
    note = ("second paquet parallèle : %s ; lots rendus à la file, prochain "
            "paquet" % why)
    try:
        work.note(db, int(lot), PARALLEL_CONFLICT + why, actor=actor)
    except Exception:  # noqa: BLE001 - la trace ne change pas la clôture
        pass
    try:
        work.close(db, int(lot), abandoned=True, note=note, actor=actor)
    except Exception:  # noqa: BLE001 - un agent lira la note du porteur
        pass


def _gate_repo(cfg, db, row, slug: str, checkout: str = "") -> str:
    """Le clone où lire le delta de la cible, résolu comme la porte des avis.

    #3534 (relecture cx3 B3) : `ci run`/`arm` laissent `checkout` vide ; le
    registre de l'assigné et le clone du projet (`radar.projects`) sont les
    mêmes sources que `_verdicts`, et doivent être lues **avant** la garde du
    delta, pas seulement plus tard par la porte des avis."""
    repo = None
    try:
        repo = fire._repo_of(db, row)
        if repo is not None and not fire._same_repository(repo, slug):
            repo = None
        project = fire._project(cfg, repo, slug)
        if repo is None:
            repo = fire._project_checkout(project)
    except Exception:  # noqa: BLE001 - registre/radar illisibles : repli
        repo = None
    if repo is None and checkout:
        repo = fire._checkout(checkout)
        if repo is not None and not fire._same_repository(repo, slug):
            repo = None
    return repo or ""


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------

def _error(proc) -> str:
    return ((proc.stderr or proc.stdout or "").strip()[:300]
            or "exit %d" % proc.returncode)


def pr_status(slug: str, number: int, *, gh) -> dict:
    """La PR `number`, dans la forme que lit `fire.ready_pr` (état, tête,
    cible, brouillon, CI, rollup, branche) plus `auto` et la cible résolue
    (`base_oid`, #3091) ; `land.LandError` sinon.

    #3308 : `baseRefOid` n'existe pas dans `gh pr view --json` avant gh 2.46
    (le poste h-05 a 2.45) ; demander ce champ faisait échouer toute la
    lecture et l'armement. La branche de base se lit sans champ optionnel,
    puis `land.base_oid` la résout par l'API. Un ref illisible refuse :
    aucune cible n'est devinée."""
    from . import radar
    proc = gh(["pr", "view", str(int(number)), "--repo", slug, "--json",
               "state,headRefOid,headRefName,baseRefName,isDraft,"
               "autoMergeRequest,statusCheckRollup,title"])
    if proc.returncode != 0:
        raise land.LandError("gh pr view %d: %s" % (int(number), _error(proc)))
    try:
        data = json.loads(proc.stdout or "")
    except ValueError as exc:
        raise land.LandError("gh pr view %d: unreadable output" % int(number)) from exc
    if not isinstance(data, dict):
        raise land.LandError("gh pr view %d: unexpected output" % int(number))
    base = str(data.get("baseRefName") or "").strip()
    state = str(data.get("state") or "").upper()
    # #3091 : la cible résolue de la PR, la seule lecture fraîche que la porte
    # des avis puisse opposer à la ref locale du checkout. Une PR fermée ou
    # fusionnée ne fusionnera plus : sa base n'a pas à être résolue.
    base_oid = land.base_oid(slug, base, gh=gh) if state == "OPEN" else ""
    rollup = radar.merge_rollup(data.get("statusCheckRollup"), slug=slug,
                                head=data.get("headRefOid") or "",
                                branch=data.get("headRefName") or "", gh=gh)
    # `ci` garde son sens strict (file/affichage) ; `ci_rule` porte la règle
    # owner du 23/10 (#3085) que lit `fire.ready_pr`.
    pending = any((c.get("status") or "COMPLETED") != "COMPLETED"
                  or (c.get("state") or "") == "PENDING" for c in rollup)
    ci = "green" if radar.ci_green(rollup) else "pending" if pending or not rollup else "red"
    ci_rule = radar.ci_state(rollup)
    return {"number": int(number), "state": state,
            "head": str(data.get("headRefOid") or "").strip().lower(),
            "base": base, "base_oid": base_oid,
            "branch": str(data.get("headRefName") or "").strip(),
            "draft": bool(data.get("isDraft")), "ci": ci, "ci_rule": ci_rule,
            "rollup": rollup,
            "auto": data.get("autoMergeRequest") is not None,
            "title": str(data.get("title") or "")}


# --------------------------------------------------------------------------
# publication
# --------------------------------------------------------------------------

def arm(cfg, db, lot: int, *, slug: str, number: int, sha: str, target: str, gh,
        actor: str = work.SYSTEM_SENDER, checkout: str = "") -> dict:
    """Arme la fusion du porteur `lot` (PR `number` de `slug`) sur `sha`,
    vers `target`.

    Rend `{state, why, merge?}` ; `state` vaut `armed`, `already-armed`,
    `unprotected` (aucun check imposé par la forge : rien d'armé, le feu est
    le geste), `moved` (la PR n'est plus sur `sha`) ou `error` ; `merge` est
    l'issue de la tentative immédiate (`fusionné` si la composition était
    déjà verte). Hors `already-armed`, chaque issue est journalisée sur le
    lot ; aucune n'est fatale à la demande de CI qui la précède."""
    sha = str(sha or "").strip().lower()
    number = int(number)
    if not _SHA.fullmatch(sha):
        return {"state": "error", "why": "the frozen head %r is not a full commit id" % sha}
    handle = _lock(db, lot)
    if handle is None:
        return {"state": "error", "why": "another auto-merge operation holds lot #%d: "
                                         "rerun `ameesh ci run %d --full`" % (lot, lot)}
    try:
        # L165 (EP4) : l'armement est un geste de fusion du pilote (il ouvre
        # le chemin de `merge_into`) ; sans identité de rôle, `gate` rend None
        # et le passage d'exécuteur reste inchangé. Le merge immédiat reçoit
        # la même garde et la renouvelle juste avant l'appel.
        from . import pilot_gesture
        try:
            fence = pilot_gesture.gate(cfg, db, "merge", "auto-merge arm #%d" % lot)
        except pilot_gesture.GestureRefused as exc:
            return {"state": "error", "why": str(exc)}
        out = _arm_locked(db, lot, slug=slug, number=number, sha=sha, target=target,
                          gh=gh, actor=actor, handle=handle, fence=fence)
        if out["state"] in ("armed", "already-armed"):
            # une composition déjà verte fusionne tout de suite, sous le même bail
            out["merge"] = _check_locked(cfg, db, lot, gh=gh, dry_run=False, actor=actor,
                                         handle=handle, fence=fence, checkout=checkout)
        return out
    finally:
        _unlock(handle)


def _arm_locked(db, lot: int, *, slug: str, number: int, sha: str, target: str, gh,
                actor: str, handle=None, fence=None) -> dict:
    lost = {"state": "error", "why": "the auto-merge lease of lot #%d was lost: rerun "
                                     "`ameesh ci run %d --full`" % (lot, lot)}

    def refuse(state: str, why: str) -> dict:
        _note_fenced(db, fence, lot, "not-armed", slug, number, sha, "— %s" % why,
                         actor)
        return {"state": state, "why": why}

    if not target:
        return refuse("error", "no target branch: the merge cannot be pinned")
    try:
        required = fire.required_checks(slug, target, None, gh=gh)
    except land.LandError as exc:
        return refuse("unprotected" if isinstance(exc, land.ForbiddenLandError) else "error",
                      "the required checks of %s cannot be read (%s): nothing to prove "
                      "the CI with, nothing armed%s"
                      % (target, exc, _fire_hint(lot, sha)
                         if isinstance(exc, land.ForbiddenLandError) else ""))
    if not required:
        return refuse("unprotected",
                      "%s has no required check enforced by GitHub: nothing to prove the "
                      "CI with, nothing armed%s" % (target, _fire_hint(lot, sha)))
    try:
        view = pr_status(slug, number, gh=gh)
    except land.LandError as exc:
        return refuse("error", str(exc))
    if view["state"] != "OPEN":
        return refuse("error", "PR #%d is %s" % (number, view["state"] or "unreadable"))
    if view["head"] != sha:
        return refuse("moved", "PR #%d head is %s, not the frozen %s"
                      % (number, view["head"][:12] or "?", sha[:12]))
    if view["base"] != target:
        return refuse("error", "PR #%d targets %s, not %s" % (number, view["base"] or "?",
                                                            target))
    if view["auto"]:
        # B1 (#1656, `fire` P1-4) : une auto-fusion GitHub fusionnerait la
        # prochaine tête poussée ; ameesh fusionne lui-même, sur la tête publiée.
        if not _renew(handle):
            return lost
        if not _fenced_hold(db, fence, margin_s=AUTOMERGE_CALL_MARGIN_S):
            return {"state": "error", "why": "the elected pilot lease was lost before "
                                             "disarming the GitHub auto-merge: nothing armed"}
        error = land.pr_disable_auto(slug, number, gh=gh)
        if error is not None:
            return refuse("error", "the GitHub auto-merge armed on PR #%d could not be "
                                   "disarmed (%s): ameesh merges the published head itself"
                          % (number, error))
    mine = active(marks(db, lot))
    if mine and (mine["sha"], mine["pr"], mine["repo"], mine["base"]) == \
            (sha, number, slug, target):
        return {"state": "already-armed",
                "why": "already armed on %s → %s" % (sha[:12], target)}
    names = ",".join(dict.fromkeys(quote(context, safe="")
                                   for context, _app in required))
    # #3534 : la base de composition est épinglée avec la tête et la cible ;
    # le passage compare la cible courante à cette borne, pas à elle-même.
    composition = (_composition_base(db, lot, sha, target)
                   or str(view.get("base_oid") or "").strip().lower())
    pinned = "base=%s method=%s required=%s" % (target, METHOD, names)
    if composition:
        pinned += " composition=%s" % composition
    if not _renew(handle):
        return lost
    if not _note_fenced(db, fence, lot, "armed", slug, number, sha,
                        "%s — ameesh merges this head into %s once its required checks "
                        "are green; a new push, a retarget or a target that advanced "
                        "since composition disarms it" % (pinned, target), actor,
                        margin_s=AUTOMERGE_CALL_MARGIN_S):
        return {"state": "error", "why": "the elected pilot lease was lost before "
                                         "arming: nothing armed"}
    return {"state": "armed", "why": "ameesh merges %s into %s once %s is green"
            % (sha[:12], target, names)}


def _fire_hint(lot: int, sha: str) -> str:
    return (" — give the pilot's fire instead: ameesh work fire %d --sha %s"
            % (int(lot), sha[:12]))


# --------------------------------------------------------------------------
# passage : désarmer (tête ou cible changée) ou fusionner (vert)
# --------------------------------------------------------------------------

def merge_into(slug: str, target: str, sha: str, *, number: int, branch: str, title: str,
               gh) -> tuple:
    """R2 B2 (ameesh-cx2) : LE geste de fusion, lié à la cible épinglée.

    `gh pr merge --match-head-commit` ne lie que la tête : la PR reciblée
    juste avant l'appel fusionnerait dans une autre branche. L'API « merge a
    branch » (`POST repos/<slug>/merges`) nomme la branche écrite (`base`) et
    le commit fusionné (`head`, la tête publiée) : un reciblage de la PR ne
    redirige pas l'écriture, une poussée sur la branche du porteur ne change
    pas le commit fusionné. GitHub marque la PR fusionnée quand sa tête entre
    dans sa base. Comme la poussée de `merge_pr`, l'écriture ne passe pas par
    la porte de la PR : les checks requis sont prouvés par ameesh juste avant.

    Rend `(commit de fusion, None)`, `("", None)` quand la tête est déjà
    contenue dans la cible (204), ou `(None, erreur)`."""
    owner = slug.split("/", 1)[0]
    text = "Merge pull request #%d from %s/%s\n\n%s%sMerged by ameesh at the published " \
           "head %s into %s (packet auto-merge, #2813).\n" % (
               int(number), owner, branch, title, "\n\n" if title else "", sha, target)
    proc = gh(["api", "-X", "POST", "repos/%s/merges" % slug, "-f", "base=%s" % target,
               "-f", "head=%s" % sha, "-f", "commit_message=%s" % text], timeout=120.0)
    if proc.returncode != 0:
        return None, _error(proc)
    out = (proc.stdout or "").strip()
    if not out:
        return "", None
    try:
        data = json.loads(out)
    except ValueError:
        return None, "unreadable merge response: %s" % out[:200]
    commit = str((data or {}).get("sha") or "").strip().lower() if isinstance(data, dict) \
        else ""
    if not _SHA.fullmatch(commit):
        return None, "merge response without a commit: %s" % out[:200]
    return commit, None


def _verdicts(cfg, db, row: dict, milestones: list, *, slug: str, base: str,
              base_sha: str = "", checkout: str = "", branch: str = "") -> tuple:
    """`fire.ready_verdicts` sur le porteur, avec le dépôt et le projet que le
    feu résout (dossier de l'assigné s'il porte ce dépôt, sinon le clone
    déclaré du projet, sinon `checkout`, le clone que le passage de l'hôte
    déclare, `packet_watch.projects.<p>.repo`). Rend `(ok, raison, projet)` ;
    le projet résolu sert ensuite à toute la porte de CI (R2, cx3). Une
    erreur refuse.

    `base_sha` (#3091) est la cible résolue par `land.base_oid` (#3308, la
    branche `baseRefName` de la PR résolue par l'API) : elle prouve la base
    de composition au lieu de dépendre de la fraîcheur de la ref locale du
    checkout.

    #3175 : sur ameesh-agents-2, le dossier de l'assigné (sur agents-1) est
    absent et aucun `radar.projects` n'est déclaré ; la porte lisait git sans
    dépôt et refusait chaque tour (« cannot read the composition members: git
    not found »). Le clone du passage sert de repli, et un hôte sans aucun
    clone le dit. Ce clone n'est celui d'aucun agent : la tête du porteur
    (`branch`) et la cible y sont lues fraîches (`_fetch_into`) avant la
    porte, qui ne fetch pas elle-même."""
    try:
        repo = fire._repo_of(db, row)
        if repo is not None and not fire._same_repository(repo, slug):
            repo = None
        project = fire._project(cfg, repo, slug)
        if repo is None:
            repo = fire._project_checkout(project)
        if repo is None and checkout:
            repo = fire._checkout(checkout)
            if repo is not None and not fire._same_repository(repo, slug):
                repo = None
            if repo is not None:
                error = _fetch_into(repo, (branch, base))
                if error:
                    return (False, "the pass clone %s cannot be refreshed for the review "
                                   "gate: %s" % (repo, error), project)
        if repo is None:
            return (False, "no checkout of %s on this host to read the review gate: the "
                           "assignee's folder is not here, and neither radar.projects.<p>.repo "
                           "nor packet_watch.projects.<p>.repo names a clone of it" % slug,
                    project)
        extra = {"base_sha": base_sha} if str(base_sha or "").strip() else {}
        ok, why, _klass = fire.ready_verdicts(cfg, db, row, milestones, repo=repo,
                                              project=project, target=base, **extra)
    except Exception as exc:  # noqa: BLE001 - une porte illisible ne fusionne pas
        return (False, "the review gate cannot be read: %s: %s" % (type(exc).__name__, exc),
                None)
    return ok, why, project


def _fetch_into(repo: str, branches) -> str:
    """Rapatrie les objets des branches `branches` d'`origin` dans `repo`
    (aucune branche locale déplacée, seules les refs de suivi `origin/*` ;
    aucun `--depth`, canon §7) ; rend l'erreur, ou "" (#3175)."""
    refs = ["refs/heads/%s" % name for name in branches if name]
    if not refs:
        return ""
    try:
        proc = subprocess.run(["git", "-C", repo, "fetch", "--quiet", "--no-tags",
                               "origin"] + refs, capture_output=True, text=True,
                              timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "exit %d" % proc.returncode).strip()[:300]
    return ""


def _required_ci(view: dict, required, project) -> dict:
    """#3066 : la CI du paquet porte sur les exigences, pas le rollup entier.

    Garder les noms exacts imposés par la forge et le projet, les statuts
    requis par préfixe et la preuve du portier (#2876). `ready_pr` contrôle
    ensuite leur présence, leur succès et leur provenance sur la tête gelée.
    Un macOS facultatif en attente ou rouge ne retient pas la fusion.
    """
    from . import radar
    names = {name for name, _app in fire._required_pairs(required)}
    prefixes = radar.required_statuses(project, view.get("branch"))
    rollup = []
    for entry in view.get("rollup") or ():
        context = (entry.get("__typename") == "StatusContext"
                   or "state" in entry and "conclusion" not in entry)
        name = entry.get("context" if context else "name") or ""
        if (name in names or context and name.startswith(prefixes)
                or entry.get("workflowName") == "paquet"
                or name == "paquet (run proof unavailable)"):
            rollup.append(entry)
    return dict(view, rollup=rollup, ci="green" if radar.ci_green(rollup) else "pending")


def _still_consigned(consigned: list, view: dict, fresh: dict, required, project) -> list:
    """#5219 : un check du portier consigné sur la première lecture de la PR ne
    l'est plus si la relecture d'avant le geste ne le consigne plus.

    `tests` lié à son application se prouve sur la provenance, lue après les
    verdicts ; le rollup de `view` peut le montrer encore en cours (paquet
    #3944 : fin de `tests` à 02:20:40, fusion à 02:21:08). Le portier remplacé
    (#4812) se lisait alors comme non concluant et ouvrait deux lots. Seul le
    portier est relu : les autres non concluants restent consignés."""
    def proof(state):
        return set(fire._packet_proof_gate(
            _required_ci(state, required, project)["rollup"])[1])
    gone = proof(view) - proof(fresh)
    return [text for text in consigned if text not in gone]


def open_carriers(db, app: str) -> list[int]:
    """Les porteurs ouverts du projet `app`."""
    st = storage.of(db).work
    found = []
    for state in OPEN_STATES:
        found += [int(row["id"]) for row in st.items(state=state, assignee=None, limit=None)
                  if carrier(row.get("branch")) and (row.get("app") or "") == app]
    return sorted(set(found))


def check(cfg, db, lot: int, *, gh, dry_run: bool = False,
          actor: str = work.SYSTEM_SENDER, checkout: str = "") -> dict:
    """Un porteur armé : le désarme si sa tête ou sa cible a changé, le
    fusionne si ses checks requis sont verts sur la tête publiée.

    `result` : `idle` (rien d'armé), `pending` (checks pas encore verts),
    `closed` (PR fusionnée ou fermée ailleurs), `disarmed`/`would-disarm`,
    `merged`/`would-merge`, `busy` (une publication tient le bail : au passage
    suivant) ou `error` ; un `pending` ou un `error` d'un porteur armé laisse
    son motif au journal du lot (`held …`, #3175)."""
    if not active(marks(db, lot)):
        return {"lot": lot, "result": "idle"}
    from . import pilot_gesture
    try:
        fence = (None if dry_run else
                 pilot_gesture.gate(cfg, db, "merge", "auto-merge check #%d" % lot))
    except pilot_gesture.GestureRefused as exc:
        return {"lot": lot, "result": "error", "error": str(exc)}
    handle = None if dry_run else _lock(db, lot)
    if handle is None and not dry_run:
        return {"lot": lot, "result": "busy"}
    try:
        return _check_locked(cfg, db, lot, gh=gh, dry_run=dry_run, actor=actor,
                             handle=handle, fence=fence, checkout=checkout)
    finally:
        if handle is not None:
            _unlock(handle)


#: les issues du passage qui refusent la fusion d'un porteur armé (#3175)
REFUSALS = ("pending", "error")


def _check_locked(cfg, db, lot: int, *, gh, dry_run: bool, actor: str,
                  handle=None, fence=None, checkout: str = "") -> dict:
    # relu sous le bail : une publication a pu réarmer depuis la première lecture
    rows = marks(db, lot)
    mine = active(rows)
    if mine is None:
        return {"lot": lot, "result": "idle"}
    out = _gate_locked(cfg, db, lot, mine, gh=gh, dry_run=dry_run, actor=actor,
                       handle=handle, fence=fence, checkout=checkout)
    if out.get("result") in REFUSALS and not dry_run:
        _held(db, lot, mine, rows, out, actor=actor, fence=fence)
    return out


def _held(db, lot: int, mine: dict, rows, out: dict, *, actor: str, fence=None) -> None:
    """#3175 : consigne au lot le motif d'un refus du passage, une fois par
    motif et par armement. Sur #2409 (h-1500), le passage refusait à chaque
    tour sans rien écrire, et le bureau a fusionné à la main sans trace du
    pourquoi. Le journal est une trace : son échec ne change pas l'issue."""
    why = " ".join(str(out.get("why") or out.get("error") or "no reason given").split())
    if out["result"] == "error":
        why = "error: " + why
    last = None
    for row in rows or ():
        if row["id"] < mine["id"]:
            continue
        if row["state"] == "held" and row["pr"] == mine["pr"] and row["sha"] == mine["sha"]:
            last = row["why"]
    if last == why:
        return
    try:
        _note_fenced(db, fence, lot, "held", mine["repo"], mine["pr"], mine["sha"],
                     "base=%s — %s" % (mine["base"], why), actor)
    except Exception:  # noqa: BLE001 - la trace ne fait jamais tomber le passage
        pass


def _masked_green(entry: dict) -> dict:
    """La même entrée, verte : relit les portes de `fire` comme si le rouge
    hérité prouvé n'existait pas. Un statut de commit porte `state`, un check
    run `status`/`conclusion`."""
    row = dict(entry)
    if row.get("__typename") == "StatusContext" \
            or "state" in row and "conclusion" not in row:
        row["state"] = "SUCCESS"
    else:
        row["status"] = "COMPLETED"
        row["conclusion"] = "SUCCESS"
    return row


def _flux_part(name: str, prefix: str) -> str | None:
    """La part d'un check `tests (flux) <part>`, ou None si ce n'est pas un
    flux de test (le préfixe est celui du veilleur, `ci.CHECK_PREFIX`)."""
    head = prefix + " "
    return name[len(head):] if name.startswith(head) and len(name) > len(head) else None


def _fresh_run(slug: str, rid: int, *, gh) -> dict | None:
    """L'état frais d'une exécution GitHub (`status`, `conclusion`,
    `run_attempt`, `head_sha`), ou None.

    #3558 (ameesh-cx3 B/P1) : la base peut être en retard sur la forge — une
    ligne encore `running`, ou un flux d'un autre attempt. Une lecture
    impossible ne prouve rien : la porte reste fermée."""
    try:
        proc = gh(["api", "repos/%s/actions/runs/%d" % (slug, int(rid))])
    except Exception:  # noqa: BLE001 - une lecture illisible ne prouve rien
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _fresh_events(slug: str, head: str, rid: int, attempt: int, *, gh) -> list[dict] | None:
    """Les événements d'échec **terminaux** de l'attempt `attempt` de
    l'exécution `rid`, relus sur la tête exacte.

    #3558 (ameesh-cx3 B/P1) : `st.ci.failures` peut ne pas encore porter le
    dernier échec publié, même quand le veilleur a conclu la ligne. Relire la
    sortie des flux terminaux et la comparer aux lignes en base prouve que le
    relevé est complet, au lieu de supposer que la dernière passe a tout vu.

    La lecture est celle du veilleur (`commits/<sha>/check-runs`, 100 par
    page) ; #3593 : toutes les pages sont lues (`--paginate`), pas seulement
    la première — au-delà de 100 check-runs, la preuve restait sinon inerte.
    Une lecture partielle (pages absentes, `total_count` non couvert, compte
    qui change d'une page à l'autre) ne prouve pas la complétude et rend
    None. None aussi si une sortie de flux porte le marqueur d'omission de
    `ci-flow.py` (relectures B/P1 du 25/10) : les échecs coupés par `MAX_TEXT`
    ne sont ni publiés ni lus, donc `events_of` les ignore et le relevé
    semblerait complet à tort."""
    from . import ci as ci_mod
    path = "repos/%s/commits/%s/check-runs?per_page=100" % (slug, quote(head, safe=""))
    try:
        pages = fire._api_pages(gh, path, "gh api %s" % path)
    except Exception:  # noqa: BLE001 - une lecture illisible ne prouve rien
        return None
    runs = []
    total = None
    for data in pages:
        if not isinstance(data, dict) or not isinstance(data.get("check_runs"), list):
            return None
        count = data.get("total_count")
        if type(count) is not int or count < 0:
            return None
        if total is None:
            total = count
        elif count != total:
            # Un check est né entre deux pages : l'ensemble lu n'est pas
            # celui que `total_count` décrit, la preuve est incomplète.
            return None
        runs.extend(run for run in data["check_runs"] if isinstance(run, dict))
    if total is None or len(runs) != total:
        return None
    for run in runs:
        if not str(run.get("name") or "").startswith(ci_mod.CHECK_PREFIX):
            continue
        output = run.get("output")
        text = output.get("text") if isinstance(output, dict) else ""
        if isinstance(text, str) and OMITTED_EVENTS_RE.search(text):
            return None
    return ci_mod.events_of(runs, int(rid), int(attempt))


def _inherited_gate(pairs, rollup, provenance, *, slug: str, gh, head: str,
                    target: str) -> tuple:
    """#4549 (B3 de dev1, confirmé par cx2) : la porte du paquet porte les
    mêmes preuves que le feu — workflow, tête et base de la cible — pour le
    remplacement d'une suite échouée au plan par une suite verte sans test.
    Sans ces preuves, `fire._supersede_testless_runs` ne remplace rien (le
    doute ne verdit jamais) ; l'appel sans elles de la version précédente
    laissait une suite verte d'un autre workflow ou sur une base périmée
    masquer un rouge."""
    return fire._required_gate(pairs, rollup, provenance, strict=False,
                               slug=slug, gh=gh, head=head, target=target)


def _inherited_red(db, *, slug: str, base: str, sha: str, view: dict, required,
                   project, gh) -> dict | None:
    """#3558 : la preuve qu'une tête n'est rouge que d'échecs **hérités** de
    la cible, ou None.

    Le veilleur marque chaque échec `inherited` quand son test était déjà
    rouge au dernier relevé conclu de la cible (`ci.py`, `st.ci.failures`).
    `fire.ready_pr` exige pourtant tous les checks requis verts : un paquet
    dont le seul rouge est ce passif restait `pending`, et un humain forçait
    la fusion à la main (h-18 #3510, h-19 #3538). Le §3 du canon (« seul un
    test négatif bloque » ; un test dépendant de l'environnement se corrige à
    la source) et le §11 bis (« rouge après fusion : corriger en avant »)
    demandent de fusionner ce paquet en journalisant l'héritage.

    La preuve est fermée par défaut :

    * la ligne du veilleur est conclue `failure`, et la forge dit le même
      run `completed` en échec, sur **le même attempt** et la même tête : un
      flux d'un autre attempt, ou une ligne encore `running`, ne prouve rien ;
    * les événements terminaux relus sur la tête pour cet attempt sont
      exactement ceux enregistrés (part, test) : un dernier échec publié après
      la passe du veilleur bloque tant qu'il n'est pas lu ;
    * aucune sortie de flux ne porte le marqueur d'omission de `ci-flow.py`
      (`MAX_TEXT`) : des échecs coupés ne sont jamais publiés, donc le relevé
      semblerait complet à tort (relectures B/P1 du 25/10) ;
    * chaque échec lu sur la tête porte `inherited`, et son test figure encore
      dans le dernier relevé conclu de la cible (un marqueur périmé, une base
      devenue verte ou sans relevé conclu ne rouvrent rien) ;
    * au moins un échec de **test** est identifié avec sa part (une ligne
      `job` ne suffit pas) ;
    * chaque check rouge de la même exécution que le check requis a un test lu
      — une part rouge sans test identifié reste bloquante ; l'exemption par
      part ne vaut que pour un agrégat requis reconnu (`tests`), tout autre
      check requis rouge (`lint`) a sa propre part et son test ;
    * chaque part lue exige son flux `tests (flux) <part>` présent et
      `FAILURE` : un flux CANCELLED (runner mort avant sa ligne `fin`) ne
      prouve pas la fin de la part et bloque, comme un flux absent ou en cours ;
    * tout check de cette exécution qui n'est ni vert, ni sauté/neutre, ni
      rouge identifié (en attente, annulé, expiré, jamais conclu) bloque ;
    * les écarts hors CI de `fire.ready_pr` (brouillon, cible, gel, statuts
      exigés) sont relus : seul un rouge de CI peut être rouvert. Un statut
      exigé non concluant (`ERROR`) ne rouvre rien mais se consigne (#3593 N1),
      comme sur le chemin vert (`#3085`, règle owner 23/10).

    Le masquage est lié aux identités exactes `(nom, exécution)` : une entrée
    de provenance homonyme d'une autre exécution (même application requise)
    reste un rouge non prouvé (relecture ameesh-cx3 B/P1).

    Rend `{parts, tests, base_shas, runs}` (matière de la note) et, s'il y en
    a, `consigned` (statuts exigés non concluants à consigner), ou None."""
    from . import ci as ci_mod, radar
    try:
        st = storage.of(db).ci
        filtered = _required_ci(view, required, project)
    except Exception:  # noqa: BLE001 - une preuve illisible ne fusionne pas
        return None
    rollup = filtered.get("rollup") or ()
    reds = [entry for entry in radar.latest_rollup(rollup)
            if radar.check_result(entry) == radar.CI_NEGATIVE_RESULT]
    if not reds:
        return None
    head = str(view.get("head") or "").strip().lower()
    if head != str(sha or "").strip().lower() or view.get("draft"):
        return None
    if base and view.get("base") and view["base"] != base:
        return None
    pairs = fire._required_pairs(required)
    required_names = {name for name, _app in pairs}
    if not pairs:
        # La fusion auto n'est armée que sur une protection prouvée (#2813) ;
        # sans check requis, il n'y a pas de rouge de CI à rouvrir ici.
        return None
    # #3558 (ameesh-cx1, B/P1) : seul un agrégat reconnu est exempté de la
    # preuve par part ; `lint` ou tout autre check requis rouge doit avoir sa
    # part et son test lus, sinon il ne se masque pas en vert.
    aggregates = required_names & INHERITED_RED_AGGREGATES
    prefix = ci_mod.CHECK_PREFIX
    provenance = None
    if any(app is not None for _, app in pairs):
        try:
            provenance = fire._head_check_runs(slug, head, gh=gh)
        except land.LandError:
            return None
    status_consigned: list[str] = []
    try:
        statuses = radar.required_statuses(project, view.get("branch"))
        status_gaps = []
        if statuses:
            entries = [entry for entry in rollup
                       if isinstance(entry, dict)
                       and (entry.get("__typename") == "StatusContext"
                            or "state" in entry and "conclusion" not in entry)]
            status_gaps, status_consigned = fire._required_status_gate(entries, statuses)
    except Exception:  # noqa: BLE001 - une porte illisible ne fusionne pas
        return None
    if status_gaps:
        # `ready_pr` refuse aussi sur un statut exigé : hors CI, rien n'est
        # rouvert (le brouillon, la cible et le gel sont relus plus haut).
        return None
    explained_names: set[str] = set()
    explained_runs: set[tuple[str, int]] = set()
    explained_rest: set[str] = set()
    seen: set[int] = set()
    bases: dict[int, dict] = {}
    parts: set[str] = set()
    tests: set[str] = set()
    for entry in reds:
        rid = radar.actions_run_id(slug, entry)
        if rid is None:
            continue
        if rid not in seen:
            seen.add(rid)
            run = st.run(slug, rid)
            if not isinstance(run, dict) \
                    or str(run.get("head_sha") or "").strip().lower() != head:
                return None
            # #3558 (ameesh-dev2, B/P1) : le veilleur CI et ce passage
            # tournent séparément. Tant que `_conclude` n'a pas figé la ligne
            # en `failure`, `st.ci.failures` peut n'avoir lu qu'un échec hérité
            # et manquer le dernier échec propre. Une ligne conclue prouve
            # qu'une dernière passe `_failures` a eu lieu (relecture cx3).
            if str(run.get("status") or "") != "failure":
                return None
            attempt = int(run.get("run_attempt") or 1)
            # #3558 (ameesh-cx3, B/P1) : la ligne peut être restée sur un
            # attempt précédent pendant qu'un flux plus récent a déjà conclu.
            # Seul un relevé de la forge terminé en échec, sur le **même
            # attempt** et la même tête, prouve le rouge lu.
            remote = _fresh_run(slug, rid, gh=gh)
            if not isinstance(remote, dict) \
                    or str(remote.get("status") or "") != "completed" \
                    or str(remote.get("conclusion") or "").lower() \
                    not in ("failure", "timed_out") \
                    or int(remote.get("run_attempt") or 1) != attempt \
                    or str(remote.get("head_sha") or "").strip().lower() != head:
                return None
            # #3558 (ameesh-cx3, B/P1) : relire les événements terminaux de cet
            # attempt et les comparer au relevé en base. Un échec publié après
            # la dernière passe du veilleur — même sur une ligne déjà conclue —
            # n'est pas encore enregistré : le masquer fusionnerait par-dessus.
            events = _fresh_events(slug, head, rid, attempt, gh=gh)
            if not events:
                return None
            failures = st.failures(slug, rid)
            if not failures or any(not f.get("inherited") for f in failures):
                return None
            recorded = {(str(f.get("part") or ""), str(f.get("test") or ""))
                        for f in failures}
            terminal = {(str(ev.get("part") or ""), str(ev["test"]))
                        for ev in events}
            if recorded != terminal:
                return None
            workflow = str(run.get("workflow") or "")
            reference = st.base_failures(slug, base, workflow=workflow)
            if not isinstance(reference, dict) or not reference.get("run_id") \
                    or not str(reference.get("head_sha") or "").strip():
                return None
            known = {str(test) for test in reference.get("tests") or ()}
            if any(str(f.get("test") or "") not in known for f in failures):
                return None
            # Une ligne `job` (panne sans événement de test, `ci.JOB_PART`)
            # n'identifie aucun test : la compter rouvrirait la fusion sur une
            # preuve absente.
            run_parts = {str(f.get("part") or "") for f in failures
                         if f.get("part") and str(f.get("part")) != ci_mod.JOB_PART
                         and f.get("test")}
            if not run_parts:
                return None
            # Chaque check de l'exécution est relu : une part rouge sans test
            # identifié, une part encore en cours ou annulée/expirée bloque.
            # #3558 (ameesh-cx1, B/P1) : l'exemption ne vaut que pour un
            # agrégat requis reconnu (`tests`) ; tout autre check requis rouge
            # — `lint`, par exemple — exige sa part et son test propres. Le
            # saut de tous les `required_names` le masquait en vert sans preuve.
            red_parts = set()
            for other in view.get("rollup") or ():
                if radar.actions_run_id(slug, other) != rid:
                    continue
                name = str(other.get("name") or other.get("context") or "")
                if name in aggregates:
                    # L'agrégat se prouve par les parts, pas par un test à son
                    # nom : il n'a pas de part propre.
                    continue
                result = radar.check_result(other)
                if result == radar.CI_NEGATIVE_RESULT:
                    if name not in run_parts:
                        return None
                    red_parts.add(name)
                elif result == radar.CI_WAITING_RESULT:
                    return None
                elif result == radar.CI_INCONCLUSIVE_RESULT:
                    state = str(other.get("conclusion") or other.get("state") or "").upper()
                    if state not in ("SKIPPED", "NEUTRAL"):
                        return None
            # #3558 (ameesh-dev4, B/P1) : un runner mort après un échec hérité
            # publie ses événements mais pas sa ligne `fin` ; le flux de la
            # part conclut alors CANCELLED — un non concluant que le portier
            # se contente de consigner, et la part lue masquerait le job rouge.
            # Chaque part de `run_parts` exige donc son flux présent et
            # FAILURE ; absent, annulé, expiré ou en cours bloque.
            # #3558 (ameesh-dev4, N) : lire le rollup complet de la tête, pas
            # le rollup filtré : un flux de part reste une preuve même quand
            # `_required_ci` ne l'a pas retenu (nom hors exigences).
            flux_results: dict[str, list[str]] = {}
            for other in radar.latest_rollup(view.get("rollup") or ()):
                part = _flux_part(str(other.get("name")
                                      or other.get("context") or ""), prefix)
                if part is not None:
                    flux_results.setdefault(part, []).append(
                        radar.check_result(other))
            for part in run_parts:
                results = flux_results.get(part)
                if not results or any(result != radar.CI_NEGATIVE_RESULT
                                      for result in results):
                    return None
            if not red_parts and not (run_parts & required_names):
                # Un agrégat rouge sans aucune part rouge lue n'est pas un
                # échec de test identifié : le cas de #3558 ne s'applique pas.
                return None
            # L'incident est celui de l'exécution de **base** dont les échecs
            # sont hérités, pas du run du paquet.
            bases[int(reference["run_id"])] = {
                "head_sha": str(reference.get("head_sha") or ""),
                "workflow": workflow, "url": ""}
            parts |= run_parts
            tests |= {str(f.get("test")) for f in failures if f.get("test")}
        name = str(entry.get("name") or entry.get("context") or "")
        explained_names.add(name)
        explained_runs.add((name, int(rid)))
    if not seen or not bases or not parts:
        return None
    if not any(name in explained_names for name in required_names):
        # Sans un check **requis** rouge et prouvé, il n'y a pas de porte de
        # paquet à rouvrir : un flux ou un détail rouge seul n'est pas le cas
        # de #3558.
        return None
    # Les rouges sans exécution Actions (le détail de l'agrégat et les flux)
    # ne se prouvent que par la part qu'ils nomment, ou par l'agrégat prouvé.
    for entry in reds:
        name = str(entry.get("name") or entry.get("context") or "")
        if radar.actions_run_id(slug, entry) is not None:
            continue
        if name == radar.AGGREGATE_DETAIL_CHECK:
            explained_rest.add(name)
            explained_names.add(name)
            continue
        part = _flux_part(name, prefix)
        if part is None or part not in parts:
            return None
        explained_rest.add(name)
        explained_names.add(name)
    # La relecture qui décide : les seules entrées masquées sont les rouges
    # prouvés ci-dessus. Tout autre écart de `ready_pr` — check absent, en
    # attente, jamais conclu, portier rouge non expliqué — reste dans les
    # portes relues et garde la fusion fermée, même à côté d'un héritage réel.
    # #3558 (ameesh-cx3, B/P1) : l'identité est exacte — `(nom, exécution)` —
    # jamais le seul nom. Une entrée de provenance homonyme d'une autre suite
    # ou d'un autre run (même application requise) reste un rouge non prouvé.
    def proven(entry) -> bool:
        name = str(entry.get("name") or entry.get("context") or "")
        rid = radar.actions_run_id(slug, entry)
        if rid is None:
            return name in explained_rest
        return (name, int(rid)) in explained_runs

    def masked(entry):
        if proven(entry) and radar.check_result(entry) == radar.CI_NEGATIVE_RESULT:
            return _masked_green(entry)
        return entry

    def masked_run(run):
        if str(run.get("conclusion") or "").upper() == "FAILURE" and proven(run):
            return _masked_green(run)
        return run

    masked_rollup = [masked(entry) for entry in rollup]
    masked_provenance = ([masked_run(run) for run in provenance]
                         if provenance is not None else None)
    try:
        gaps, _consigned = _inherited_gate(pairs, masked_rollup,
                                           masked_provenance, slug=slug,
                                           gh=gh, head=sha, target=base)
        proof_gaps, _proof_consigned = fire._packet_proof_gate(masked_rollup)
        detail_gaps, _detail_consigned = fire._aggregate_detail_gate(masked_rollup)
    except Exception:  # noqa: BLE001 - une porte illisible ne fusionne pas
        return None
    if gaps or proof_gaps or detail_gaps:
        return None
    proof = {"parts": sorted(parts), "tests": sorted(tests),
             "base_shas": sorted({row["head_sha"] for row in bases.values()
                                  if row.get("head_sha")}),
             "runs": bases}
    # #3593 (N1) : `ready_pr` s'arrête sur les checks rouges avant de relire
    # les statuts exigés ; le statut non concluant (ERROR) consigné ici ne
    # doit pas être jeté — il se consigne et ouvre son lot de correction comme
    # sur le chemin vert (`#3085`, règle owner 23/10).
    if status_consigned:
        proof["consigned"] = sorted(set(status_consigned))
    return proof


def _inherited_text(proof: dict) -> str:
    """La phrase de journal d'une fusion sur rouge hérité (#3558)."""
    return ("merged over inherited red: %s %s base %s"
            % (", ".join(proof["parts"]), ",".join(proof["tests"]),
               ",".join(proof["base_shas"])))


def _note_inherited_incident(db, row: dict, *, slug: str, base: str, number: int,
                             sha: str, proof: dict, actor: str) -> None:
    """#3558 : la cible reste rouge après la fusion ; l'occurrence est notée
    sur le lot d'incident **ouvert** de l'exécution de base, ou le lot est
    ouvert (même geste que `_open_correction`, #3085).

    L'écriture ne défait jamais la fusion déjà journalisée : tout échec est
    avalé, la trace est un suivi."""
    from . import incident
    occurrence = ("%s — packet #%d (PR #%d, head %s) merged by ameesh over the red "
                  "target; the target stays red until the correction lands"
                  % (_inherited_text(proof), int(row.get("id") or 0), int(number),
                     sha[:12]))
    for rid, run in (proof.get("runs") or {}).items():
        source = incident.ref_of(slug, rid)
        try:
            existing = work.open_with_source(db, source)
        except Exception:  # noqa: BLE001 - lecture impossible : on ouvre
            existing = None
        if existing is not None:
            try:
                work.note(db, int(existing["id"]), occurrence, actor=actor)
            except Exception:  # noqa: BLE001 - la trace ne défait pas la fusion
                pass
            continue
        try:
            work.add(
                db,
                title=incident.title_of(branch=base, run_id=rid,
                                        workflow=run.get("workflow") or ""),
                type="bug", priority=1, app=str(row.get("app") or ""),
                source=source, issue_ref=source, unique_ref=True,
                body=incident.body_of(
                    repo=slug, branch=base, run_id=rid,
                    head_sha=run.get("head_sha") or "",
                    workflow=run.get("workflow") or "", url=run.get("url") or "",
                    tests=proof["tests"],
                    attribution={"status": "unknown"}) + "\n\n" + occurrence,
                actor=actor)
        except Exception:  # noqa: BLE001 - l'incident existe déjà (même fermé)
            try:
                rows = storage.of(db).work.by_issue_ref(source) or []
                if rows:
                    work.note(db, int(rows[0]["id"]), occurrence, actor=actor)
            except Exception:  # noqa: BLE001 - aucun suivi possible
                pass


def _gate_locked(cfg, db, lot: int, mine: dict, *, gh, dry_run: bool, actor: str,
                 handle=None, fence=None, checkout: str = "") -> dict:
    slug, number, sha, base = mine["repo"], mine["pr"], mine["sha"], mine["base"]
    out = {"lot": lot, "pr": number, "sha": sha, "base": base}
    try:
        view = pr_status(slug, number, gh=gh)
    except land.LandError as exc:
        return dict(out, result="error", error=str(exc))
    if view["state"] != "OPEN":
        return dict(out, result="closed", state=view["state"])
    row, milestones = fire._fresh(db, lot)
    if row is None:
        return dict(out, result="error", error="lot #%d not found" % lot)
    frozen = fire._norm((fire.freeze_of(milestones) or {}).get("sha"))
    base_oid = str(view.get("base_oid") or "").strip().lower()
    composition = (_composition_base(db, lot, sha, base)
                   or str(mine.get("composition") or "").strip().lower())
    # #3534 (B3) : le clone se résout comme pour la porte des avis, avant la
    # garde du delta ; `checkout` est vide dans `ci run`/`arm`.
    repo = _gate_repo(cfg, db, row, slug, checkout)
    # #3534 : le fetch précède les gardes git (tête de la PR et cible). Il est
    # aussi nécessaire à la garde d'ordre #3256 C, qui compare la tête du
    # premier paquet à la cible fraîche.
    if repo and base_oid:
        _fetch_into(repo, (view.get("branch") or "", base))
    # #3256 C : l'ordre de la paire est une garde, même quand la cible n'a pas
    # encore bougé. Le second ne fusionne jamais avant le premier ; un premier
    # clos sans fusion rend le second à la file.
    peer = _parallel_peer(db, lot)
    if peer is not None and not dry_run:
        first_head = _first_head(db, peer)
        if not first_head or _git(repo, "merge-base", "--is-ancestor",
                                  first_head, base_oid) is None:
            first_state = _first_state(db, peer)
            if first_state == "closed":
                why = ("premier paquet #%d clos sans fusion : le second revient "
                       "en file" % peer)
                if not _renew(handle):
                    return dict(out, result="busy")
                if not _fenced_hold(db, fence, margin_s=AUTOMERGE_CALL_MARGIN_S):
                    return dict(out, result="error",
                                error="the elected pilot lease was lost before "
                                      "closing the second packet: nothing closed")
                _parallel_return_to_queue(db, lot, row, why, actor)
                return dict(out, result="closed", state="closed", why=why)
            return dict(out, result="pending",
                        why="second paquet : le premier #%d n'est pas encore "
                            "fusionné" % peer)
    moved = []
    # #3534 : la cible doit être couverte par la tête testée, soit directement
    # (H2 = merge(H, C)), soit par une avance limitée aux atterrissages de
    # coupure depuis le dernier commit de la cible déjà dans la tête (B2,
    # #173). Sinon la CI du paquet n'a pas testé cet arbre : on désarme.
    target_moved = ""
    parallel = None
    if base_oid and composition and base_oid != composition:
        if not _target_tested(repo, base_oid, sha, composition):
            target_moved = ("target advanced since composition %s → %s"
                            % (composition[:12], base_oid[:12]))
            if not dry_run:
                # #3256 C : le second d'une paire parallèle fusionne après le
                # premier, sans refaire la suite complète quand la fusion de sa
                # tête dans la cible avancée est propre et les contrôles de gel
                # verts. Un conflit le rend à la file.
                proof = _parallel_proof(db, repo, lot, composition=composition,
                                        target=base_oid, sha=sha, dry_run=dry_run)
                if proof.get("state") == "wait":
                    return dict(out, result="pending", why=proof.get("why") or
                                "second paquet : le premier n'est pas encore fusionné")
                if proof.get("state") == "inconclusive":
                    # Preserve the carrier and its units; the composer retries
                    # on the current target with full CI instead of waiting
                    # forever for this host-specific exemption proof.
                    if not _renew(handle):
                        return dict(out, result="busy")
                    if not _note_fenced(db, fence, lot, "parallel-recompose", slug,
                                        number, sha, proof.get("why") or "?", actor,
                                        margin_s=AUTOMERGE_CALL_MARGIN_S):
                        return dict(out, result="busy")
                    return dict(out, result="pending",
                                why="preuve du second paquet non concluante: %s; recomposition demandée"
                                    % (proof.get("why") or "?"))
                if proof.get("state") == "conflict":
                    if not _renew(handle):
                        return dict(out, result="busy")
                    if not _fenced_hold(db, fence, margin_s=AUTOMERGE_CALL_MARGIN_S):
                        return dict(out, result="error",
                                    error="the elected pilot lease was lost before "
                                          "closing the second packet: nothing closed")
                    _parallel_return_to_queue(db, lot, row, proof.get("why") or "conflit",
                                              actor)
                    return dict(out, result="closed", state="closed",
                                why=proof.get("why") or "conflit")
                if proof.get("state") == "ok":
                    # La marque de preuve est une mutation : elle suit le
                    # renouvellement du bail, jamais avant (avis auditeur B3).
                    if not _renew(handle):
                        return dict(out, result="busy")
                    try:
                        work.note(db, int(lot),
                                  "%s%s — merge-tree clean, freeze-checks green, "
                                  "tête testée inchangée (#3256 C)"
                                  % (PARALLEL_PROOF, proof.get("marker") or ""),
                                  actor=actor)
                    except Exception:  # noqa: BLE001 - la preuve sera rejouée
                        pass
                    parallel = proof
                    target_moved = ""
            if target_moved:
                moved.append(target_moved)
    if view["head"] != sha:
        moved.append("head=%s" % (view["head"] or "?"))
    if view["base"] != base:
        moved.append("now targets %s" % (view["base"] or "?"))
    if frozen != sha:
        moved.append("frozen=%s" % (frozen or "none"))
    if moved:
        out["moved"] = moved
        if dry_run:
            return dict(out, result="would-disarm")
        if not _renew(handle):
            return dict(out, result="busy")
        if not _fenced_hold(db, fence, margin_s=AUTOMERGE_CALL_MARGIN_S):
            return dict(out, result="error",
                        error="the elected pilot lease was lost before disarming: "
                              "nothing disarmed")
        if view["auto"]:
            error = land.pr_disable_auto(slug, number, gh=gh)
            if error is not None:
                return dict(out, result="error", error="disarm: %s" % error)
        why = ("the target advanced since composition; the packet must be updated "
               "and its CI relaunched: `ameesh ci run %d --full` re-arms on the new head"
               % int(lot)) if target_moved else (
               "the PR changed since its publication; `ameesh ci run %d --full` "
               "re-arms on the new head" % int(lot))
        if not _note_fenced(db, fence, lot, "disarmed", slug, number, sha,
                            "base=%s %s — %s" % (base, " ".join(moved), why), actor):
            return dict(out, result="error",
                        error="the elected pilot lease was lost before journaling "
                              "the disarm")
        return dict(out, result="disarmed")
    if view["auto"] and not dry_run:
        # armée à la main depuis la publication : ameesh reste seul à fusionner
        if not _renew(handle):
            return dict(out, result="busy")
        if not _fenced_hold(db, fence, margin_s=AUTOMERGE_CALL_MARGIN_S):
            return dict(out, result="error",
                        error="the elected pilot lease was lost before disarming: "
                              "nothing disarmed")
        error = land.pr_disable_auto(slug, number, gh=gh)
        if error is not None:
            return dict(out, result="error", error="disarm: %s" % error)
    # R1 B1 (cx3) : la porte complète des verdicts, comme le feu (classe,
    # avis requis et indépendants sur la tête gelée, membres d'un porteur et
    # delta, #2766), avant toute lecture de CI et tout geste de fusion.
    # #3091 : la cible résolue de la PR est la preuve transmise à
    # `fire.ready_verdicts` ; `packet_reviews._packet_base` choisit ensuite la
    # base du manifeste et la prouve sur cette cible. #3534 (B4) : ne jamais
    # transmettre la base enregistrée à sa place — un `record --base X` où X
    # est un commit sensible du paquet sortirait B..X du périmètre de revue.
    verdict_base = base_oid
    extra = {"base_sha": verdict_base} if verdict_base else {}
    if checkout:
        extra.update(checkout=checkout, branch=view.get("branch") or "")
    ok, why, project = _verdicts(cfg, db, row, milestones, slug=slug, base=base,
                                 **extra)
    if not ok:
        return dict(out, result="pending", why=why)
    # R1 B2 (cx3) : au geste, au moins un check imposé par la forge, et tous
    # ceux de la publication ; une protection allégée depuis ne fusionne rien.
    # Cette preuve se lit sans déclaration : une déclaration ne la remplace pas.
    try:
        forged = fire.required_checks(slug, base, None, gh=gh)
        # R2 (cx3) : la porte de CI garde le projet résolu — checks déclarés,
        # `required_statuses`, `ci_required` — en plus de la forge.
        required = (fire.required_checks(slug, base, project, gh=gh)
                    if project is not None else forged)
    except land.LandError as exc:
        return dict(out, result="pending",
                    why="the required checks of %s cannot be read: %s" % (base, exc))
    names = set(context for context, _app in forged)
    dropped = [name for name in mine["required"] if name not in names]
    if not forged or dropped:
        return dict(out, result="error", error=(
            "%s no longer enforces %s: nothing proves the CI the publication relied on, "
            "nothing merged" % (base, ", ".join(dropped) or "any required check")))
    report: dict = {}
    ok, why = fire.ready_pr(_required_ci(view, required, project),
                            target=base, project=project, frozen=sha,
                            required=required, slug=slug, gh=gh, report=report)
    inherited = None
    if not ok:
        # #3558 (canon §3, §11 bis) : un rouge que le paquet n'introduit pas —
        # chaque échec marqué `inherited` au dernier relevé conclu de la cible —
        # ne retient pas sa fusion. La preuve est relue ici, jamais supposée :
        # tout échec propre, une part rouge sans test lu, une base sans relevé
        # conclu ou une autre porte de `ready_pr` gardent la porte fermée.
        try:
            inherited = _inherited_red(db, slug=slug, base=base, sha=sha, view=view,
                                       required=required, project=project, gh=gh)
        except Exception:  # noqa: BLE001 - une preuve illisible ne fusionne pas
            inherited = None
        if inherited is not None:
            ok = True
    if not ok:
        # #3186 (décision owner #128512) : un check requis jamais conclu est
        # une preuve absente ; la porte reste fermée et relance son run une
        # fois, sous le bail du passage et la garde du pilote élu.
        reruns = fire.relaunch_unconcluded(
            db, lot, slug, sha, report, gh=gh, dry_run=dry_run, actor=actor,
            guard=lambda: _renew(handle) and _fenced_hold(
                db, fence, margin_s=AUTOMERGE_CALL_MARGIN_S))
        if reruns:
            from . import radar
            return dict(out, result="pending", reruns=reruns,
                        why="%s — %s" % (why, radar.describe_reruns(reruns)))
        return dict(out, result="pending", why=why)
    if dry_run:
        return dict(out, result="would-merge",
                    **({"inherited": inherited} if inherited else {}))
    # La PR est relue juste avant le geste, après toutes les lectures de la
    # porte ; une tête, une cible ou un état changés depuis ne fusionnent pas
    # (le passage suivant désarme).
    try:
        fresh = pr_status(slug, number, gh=gh)
    except land.LandError as exc:
        return dict(out, result="error", error="fresh read: %s" % exc)
    # #3534 (B1) : la cible est relue **et comparée à son SHA de la porte**,
    # pas seulement à son nom ; une avance de code pendant les lectures ne
    # fusionne pas un arbre non testé (le passage suivant désarmera).
    if (fresh["state"], fresh["head"], fresh["base"], fresh["base_oid"]) != \
            ("OPEN", sha, base, base_oid):
        return dict(out, result="pending", why=(
            "PR #%d changed just before the merge (%s, head %s, base %s, target %s — "
            "expected target %s): nothing merged"
            % (number, fresh["state"] or "?", fresh["head"][:12] or "?",
               fresh["base"] or "?", fresh["base_oid"][:12] or "?",
               base_oid[:12] or "?")))
    if not _renew(handle):
        return dict(out, result="busy")
    # L165 (EP4) : renouvellement du bail élu juste avant `POST /merges`, avec
    # la marge de l'appel ; sans rôle/identité, `fence` est None et le passage
    # d'exécuteur reste inchangé.
    if fence is not None:
        from . import pilot_gesture
        try:
            fence.hold(db, margin_s=AUTOMERGE_CALL_MARGIN_S)
        except pilot_gesture.GestureRefused as exc:
            return dict(out, result="error", error=str(exc))
    commit, error = merge_into(slug, base, sha, number=number, branch=fresh["branch"],
                               title=fresh.get("title") or "", gh=gh)
    if error is not None:
        return dict(out, result="error", error="merge: %s" % error)
    consigned = _still_consigned(list(report.get("consigned") or ()), view, fresh,
                                 required, project)
    # #3593 (N1) : les statuts exigés non concluants relus par la preuve du
    # rouge hérité (ERROR) n'ont pas été consignés par `ready_pr`, qui s'arrête
    # sur les checks rouges avant cette lecture. Ils se joignent à la liste,
    # sans doublon quand `ready_pr` les a déjà portés.
    for text in (inherited or {}).get("consigned") or ():
        if text not in consigned:
            consigned.append(text)
    verdict = "gates green" if not inherited else "gates green apart from inherited red"
    note = ("base=%s commit=%s — merged by ameesh into %s at its published head, %s"
            % (base, commit or "already-contained", base, verdict))
    if parallel:
        note += ("; second paquet parallèle : fusion de la cible avancée propre, "
                 "contrôles de gel verts, tête testée %s inchangée (#3256 C)"
                 % sha[:12])
    if inherited:
        note += "; " + _inherited_text(inherited)
    if consigned:
        note += ("; required checks non concluants consignés (règle owner 23/10) : %s"
                 % ", ".join(consigned))
    _note(db, lot, "merged", slug, number, sha, note, actor)
    if consigned:
        _open_correction(db, row, slug=slug, number=number, sha=sha, base=base,
                         consigned=consigned, actor=actor)
    if inherited:
        _note_inherited_incident(db, row, slug=slug, base=base, number=number,
                                 sha=sha, proof=inherited, actor=actor)
    return dict(out, result="merged", commit=commit, consigned=consigned,
                **({"inherited": inherited} if inherited else {}))


def _open_correction(db, row: dict, *, slug: str, number: int, sha: str, base: str,
                     consigned: list, actor: str):
    """#3085 : ouvre le lot de correction d'un check requis non concluant qui
    n'a pas retenu la fusion. Le lot est journalisé, sans assigné (file de
    l'équipe) ; son ouverture ne défait jamais la fusion déjà écrite.
    #3209 : un lot par check tant qu'il est ouvert (`radar.nonconclusive_source`) ;
    le même check au paquet suivant est noté sur ce lot."""
    from . import radar
    app = str(row.get("app") or "")
    out = []
    for text in consigned:
        try:
            out.append(work.add_or_note(
                db,
                title="CI non concluante sur %s@%s : corriger %s" % (slug, sha[:12], text),
                type="bug", priority=2, app=app,
                source=radar.nonconclusive_source(app or slug, text),
                occurrence=("de nouveau non concluant sur la tête %s du paquet %s#%d "
                            "(base %s) : %s" % (sha[:12], slug, int(number), base, text)),
                body=("Le check requis n'a pas conclu (ni vert ni négatif) sur la tête %s "
                      "du paquet %s#%d (base %s) : %s.\n\nRègle owner du 23/10 (canon home "
                      "§3) : un résultat non concluant ne bloque pas la fusion ; il se "
                      "consigne et ouvre ce lot de correction. Rejouer ou réparer le "
                      "check, sans relire le paquet déjà fusionné. Tant que ce lot est "
                      "ouvert, les occurrences suivantes y sont notées."
                      % (sha, slug, int(number), base, text)),
                actor=actor))
        except Exception:  # noqa: BLE001 - la fusion est écrite, le lot est un suivi
            continue
    return out


def watch(cfg, db, app: str, *, gh=None, dry_run: bool = False,
          checkout: str = "") -> list[dict]:
    """Tous les porteurs ouverts de `app` ; un porteur ne fait pas tomber l'autre.

    `checkout` : le clone que le passage de l'hôte déclare pour ce projet
    (`packet_watch.projects.<p>.repo`), repli de la porte des avis (#3175)."""
    gh = gh or land._gh
    results = []
    for lot in open_carriers(db, app):
        try:
            results.append(check(cfg, db, lot, gh=gh, dry_run=dry_run, checkout=checkout))
        except Exception as exc:  # noqa: BLE001 - un porteur ne fait pas tomber l'autre
            results.append({"lot": lot, "result": "error",
                            "error": "%s: %s" % (type(exc).__name__, exc)})
    return results
