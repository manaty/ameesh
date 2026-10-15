# SPDX-License-Identifier: AGPL-3.0-only
"""#1465 — le fournisseur exact d'origine d'un commit et d'un jalon.

Contrat importable pour le qualificateur du train (#1467,
`train_review.qualify`). Pour chaque sujet, il rend un reçu aux champs de
`train_review.Receipt` — `subject`, `commit`, `actor`, `session`, `turn`,
`harness`, `record`, `milestone` — ou `None` quand la source manque.

    from ameesh import review_provenance as prov
    recu = prov.receipt_for_commit(db, sha)          # ou None (inconnu)
    recu = prov.receipt_for_milestone(db, jalon_id)  # ou None (inconnu)

Ce que le fournisseur ne fait JAMAIS :

* deviner un tour par proximité temporelle (`turn_costs.recorded_at` voisin),
  par le harnais déclaré au registre, ou par le nom d'auteur Git : ce ne sont
  pas des liens, mesh-design 15/10 03:53 ;
* compléter une preuve partielle : une ligne `provenance_receipts` sans son
  fait (`work_item_milestones`) est une origine inconnue ;
* réécrire une preuve : la première écriture fait foi, une divergence d'identité
  est une erreur (`ProvenanceError`), pas un remplacement silencieux.

La preuve est écrite à l'instant du fait (migration 0445) : le commit par
`ameesh provenance record --commit` (hook post-commit), le jalon dans la même
transaction que `work_item_milestones`. Le `turn` est l'identifiant du tour
managé (`AMEESH_TURN_ID`), la `session` celle du registre vivant de l'acteur,
et le `harness` celui du tour (`AMEESH_HARNESS`), recontrôlé contre le registre.

Un commit n'est enregistrable que s'il a été **créé dans ce tour** : le hook
`pre-commit` écrit avant le commit une attestation de création (identité du
tour, HEAD et arbre de départ), que `record_commit` exige et consomme. Un HEAD
hérité d'un tour antérieur — même committé par le même agent — n'a pas
d'attestation : son origine reste inconnue (écart cx3, 15/10 04:37).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess

from . import storage

#: les familles de harnais que le qualificateur de train sait opposer
FAMILIES = frozenset({"claude", "codex", "deepseek"})
#: un identifiant de commit complet : SHA-1 (40) ou SHA-256 (64)
FULL_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
#: une abréviation Git citée par un jalon (au moins 7 caractères)
ABBREV = re.compile(r"[0-9a-f]{7,64}")


class ProvenanceError(Exception):
    """Preuve incohérente ou identité d'écriture incomplète : on refuse."""


def full_sha(value) -> str:
    """Le commit complet, en minuscules ; refuse tout le reste."""
    text = str(value or "").strip().lower()
    if not FULL_SHA.fullmatch(text):
        raise ProvenanceError("full commit id expected, got %r" % (value,))
    return text


def subject_of_commit(value) -> str:
    return "commit:" + full_sha(value)


def subject_of_milestone(milestone_id) -> str:
    try:
        number = int(milestone_id)
    except (TypeError, ValueError) as exc:
        raise ProvenanceError("milestone id expected, got %r" % (milestone_id,)) from exc
    if number <= 0:
        raise ProvenanceError("milestone id expected, got %r" % (milestone_id,))
    return "milestone:%d" % number


def _receipt(row, *, commit: str, milestone) -> dict:
    """Le reçu aux champs de `train_review.Receipt`."""
    return {
        "subject": row["subject"],
        "commit": commit,
        "actor": row["actor"],
        "session": row["session"],
        "turn": row["turn"],
        "harness": row["harness"],
        # la ligne elle-même est le reçu exact, jamais une note libre
        "record": "provenance_receipts:%s" % row["subject"],
        "milestone": milestone,
    }


def receipt_for_commit(db, sha) -> dict | None:
    """Le reçu exact du commit `sha`, ou None si son origine est inconnue.

    `sha` doit être complet : le train qualifie des commits complets, et une
    abréviation ne désigne un commit qu'en présence du dépôt — le fournisseur
    ne l'a pas."""
    subject = subject_of_commit(sha)
    row = storage.of(db).provenance.get(subject)
    if row is None:
        return None
    if row["sha"] != subject[len("commit:"):]:
        # Une ligne existe pour ce sujet mais enregistre un AUTRE commit :
        # ce n'est pas une absence (unknown), c'est une preuve corrompue.
        raise ProvenanceError(
            "%s: proof records commit %s, not the subject" % (subject, row["sha"]))
    return _receipt(row, commit=row["sha"], milestone=None)


def receipt_for_milestone(db, milestone_id) -> dict | None:
    """Le reçu exact du jalon, ou None si son origine est inconnue.

    Le reçu recopie les faits du jalon (`id`, `actor`, `at_ts`, `sha`,
    `verdict`) et porte le commit COMPLET que le `sha` déclaré résout. Une
    incohérence entre la preuve et le fait — acteur différent, abréviation qui
    ne résout pas le commit enregistré — est refusée, pas rabotée."""
    subject = subject_of_milestone(milestone_id)
    row = storage.of(db).provenance.milestone(subject)
    if row is None:
        return None
    if str(row["mark_actor"]).strip().casefold() != str(row["actor"]).strip().casefold():
        raise ProvenanceError(
            "%s: proof actor %r differs from the milestone actor %r"
            % (subject, row["actor"], row["mark_actor"]))
    mark_sha = str(row["mark_sha"] or "").strip().lower()
    if mark_sha:
        if not ABBREV.fullmatch(mark_sha) or not row["sha"].startswith(mark_sha):
            raise ProvenanceError(
                "%s: declared sha %r does not resolve the recorded commit %s"
                % (subject, row["mark_sha"], row["sha"]))
    return _receipt(row, commit=row["sha"], milestone={
        "id": row["milestone_id"],
        "actor": row["mark_actor"],
        "at_ts": row["mark_at_ts"],
        "sha": row["mark_sha"],
        "verdict": row["verdict"],
    })


def capture(environ=None, *, actor: str = "") -> dict:
    """L'identité d'écriture d'un fait, lue dans le tour managé courant.

    Refuse hors tour (`AMEESH_TURN_ID` absent) ou sur un harnais inconnu : une
    preuve sans tour ou sans famille n'est pas une preuve. Le harnais du tour
    est celui du runner (`AMEESH_HARNESS`), jamais un nom choisi par le dépôt.

    `actor` n'est qu'une **attente** : quand il est fourni, il doit être celui
    du tour (`AGENT_MAIL_NAME`, posé par le runner) — un tour ne signe pas la
    preuve d'un autre agent (écart relevé par la relecture cx3, 15/10 04:27)."""
    env = os.environ if environ is None else environ
    turn_actor = str(env.get("AGENT_MAIL_NAME") or "").strip()
    who = str(actor or turn_actor).strip()
    turn = str(env.get("AMEESH_TURN_ID") or "").strip()
    harness = str(env.get("AMEESH_HARNESS") or "").strip().lower()
    if not who:
        raise ProvenanceError("no agent identity (AGENT_MAIL_NAME)")
    if not turn:
        raise ProvenanceError("no managed turn (AMEESH_TURN_ID): outside a turn")
    if turn_actor and who != turn_actor:
        raise ProvenanceError(
            "actor %r is not the agent of this turn (%r)" % (who, turn_actor))
    if harness not in FAMILIES:
        raise ProvenanceError("unknown harness %r (expected %s)"
                              % (harness, ", ".join(sorted(FAMILIES))))
    return {"actor": who, "turn": turn, "harness": harness}


def live_session(db, actor: str, *, harness: str = "") -> str:
    """La session VIVANTE de l'acteur, au registre ; refuse si aucune.

    La preuve nomme la session qui a produit le fait : une session absente ne
    se remplace pas par un identifiant vide. Le harnais attendu est recontrôlé
    contre celui de la session vivante (`registry.live_harness`, #1037) : un
    harnais emprunté ne peut pas signer une preuve sous son étiquette."""
    from . import registry
    row = registry.get(db, actor) or {}
    session = str(row.get("session_id") or "").strip()
    if not session:
        raise ProvenanceError("no live session for %s" % actor)
    if harness:
        live = str(registry.live_harness(row) or "").strip().lower()
        if live and live != str(harness).strip().lower():
            raise ProvenanceError(
                "harness %r does not match the live session of %s (%r)"
                % (harness, actor, live))
    return session


def resolve_commit(value, *, cwd: str | None = None) -> str | None:
    """Le commit complet qu'une abréviation désigne, dans le dépôt sous la main.

    Rend None quand `git` ne résout pas — jamais une supposition : la preuve
    exige le commit complet, et une abréviation ambiguë ou inconnue laisse
    l'origine inconnue."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        run = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", text + "^{commit}"],
            capture_output=True, text=True, cwd=cwd or os.getcwd(), timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = (run.stdout or "").strip().lower()
    if run.returncode != 0 or not FULL_SHA.fullmatch(sha):
        return None
    return sha


def _git(cwd: str | None, *args: str) -> str | None:
    """La sortie de `git` sous la main, ou None (échec, git absent)."""
    try:
        run = subprocess.run(["git", *args], capture_output=True, text=True,
                             cwd=cwd or os.getcwd(), timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if run.returncode != 0:
        return None
    return (run.stdout or "").strip()


#: L'attestation de création du hook `pre-commit` vit dans le répertoire git DU
#: worktree (`--absolute-git-dir`) : les hooks sont partagés entre worktrees,
#: l'attestation ne l'est pas.
INTENT_NAME = "ameesh-provenance-intent.json"


def intent_path(cwd: str | None = None) -> str | None:
    """Le chemin de l'attestation de ce worktree, ou None hors dépôt."""
    root = _git(cwd, "rev-parse", "--absolute-git-dir")
    return os.path.join(root, INTENT_NAME) if root else None


def clear_commit_intent(cwd: str | None = None) -> None:
    """Retire l'attestation du worktree (au mieux : absente n'est pas une erreur)."""
    path = intent_path(cwd)
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def prepare_commit_intent(cwd: str | None = None, *, environ=None,
                          actor: str = "") -> dict | None:
    """Écrit l'attestation du commit qui vient (hook `pre-commit`).

    Elle lie le tour courant au HEAD et à l'arbre d'où le commit va partir.
    Elle ne bloque jamais un commit : hors tour managé, sans identité ou sans
    arbre calculable, elle n'est pas écrite (et toute attestation précédente
    est retirée) — le commit naîtra sans preuve, son origine restera inconnue.
    Aucune base n'est requise : c'est le hook `post-commit` qui écrit la preuve.
    """
    env = os.environ if environ is None else environ
    path = intent_path(cwd)
    try:
        ident = capture(env, actor=actor)
    except ProvenanceError:
        ident = None
    tree = _git(cwd, "write-tree") if ident is not None else None
    if path is None or ident is None or not tree:
        if path:
            clear_commit_intent(cwd)
        return None
    intent = {
        "version": 1,
        "actor": ident["actor"],
        "turn": ident["turn"],
        "harness": ident["harness"],
        "head": _git(cwd, "rev-parse", "--verify", "--quiet", "HEAD^{commit}") or "",
        "tree": tree,
        "nonce": secrets.token_hex(16),
    }
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(intent, fh, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        return None
    return intent


def read_commit_intent(cwd: str | None = None) -> dict | None:
    """L'attestation en cours de ce worktree, ou None si absente ou illisible."""
    path = intent_path(cwd)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("actor", "turn", "harness", "head", "tree", "nonce"):
        if not isinstance(data.get(key), str):
            return None
    return data


def _parents(cwd: str | None, sha: str) -> list[str] | None:
    """Les parents du commit, ou None s'il ne se lit pas."""
    out = _git(cwd, "rev-list", "--parents", "-n", "1", sha)
    if out is None:
        return None
    fields = out.split()
    if not fields or fields[0] != sha:
        return None
    return fields[1:]


def _attests_creation(cwd: str | None, commit: str, intent: dict) -> bool:
    """Le commit est-il exactement la création annoncée par l'attestation ?

    Structurel, jamais temporel : le commit part du HEAD attesté (commit posé
    dessus, y compris une fusion en cours résolue par `git commit`), ou en est
    la réécriture (amend : mêmes parents que le HEAD attesté), et porte l'arbre
    attesté. Le commit attesté lui-même n'est jamais sa propre création."""
    parents = _parents(cwd, commit)
    if parents is None:
        return False
    tree = _git(cwd, "rev-parse", "%s^{tree}" % commit)
    if not tree or tree != intent["tree"]:
        return False
    head = intent["head"]
    if not head:
        return not parents
    if commit == head:
        return False
    head_parents = _parents(cwd, head)
    if head_parents is None:
        return False
    if parents and parents[0] == head:
        return True
    # amend/rewrite : le commit remplace le HEAD attesté, il en reprend les parents
    return parents == head_parents


def _same_identity(row, identity: dict) -> bool:
    return (row["actor"] == identity["actor"] and row["session"] == identity["session"]
            and row["turn"] == identity["turn"] and row["harness"] == identity["harness"])


def proof_for_current_turn(db, *, actor: str = "", sha: str = "",
                           cwd: str | None = None) -> dict | None:
    """L'identité exacte du tour courant, ou None si le fait n'y est pas déclaré.

    None couvre trois cas, tous « origine inconnue » : pas de tour managé
    (`AMEESH_TURN_ID` absent), pas de session vivante au registre pour l'acteur,
    ou un `sha` déclaré que le dépôt ne résout pas. Un jalon hors tour s'écrit
    donc sans preuve — jamais avec une preuve devinée."""
    try:
        ident = capture(actor=actor)
        ident["session"] = live_session(db, ident["actor"], harness=ident["harness"])
    except ProvenanceError:
        return None
    declared = str(sha or "").strip()
    if declared:
        full = resolve_commit(declared, cwd=cwd)
        if full is None:
            return None
        ident["sha"] = full
    else:
        ident["sha"] = ""
    return ident


def record_commit(db, sha, *, identity: dict, work_item_id: int | None = None,
                  cwd: str | None = None) -> dict:
    """Écrit la preuve exacte d'un commit créé dans ce tour, ou refuse.

    C'est le point d'écriture du hook `post-commit` : l'identité vient du tour
    courant (jamais de l'appelant). Trois bornes : le commit est **HEAD du
    dépôt sous la main**, son committer est l'acteur du tour
    (`<acteur>@ameesh.invalid`, posé par le runner), et il est **attesté comme
    création de ce tour** par le hook `pre-commit` (identité du tour, HEAD et
    arbre de départ) — attestation consommée par l'écriture. Un HEAD hérité
    d'un tour antérieur, même committé par le même agent, n'a pas d'attestation
    de création : son origine reste inconnue (écart cx3, 15/10 04:37), jamais
    réattribuée au nouveau tour.

    Une preuve déjà écrite pour ce commit n'est pas réécrite : une identité
    différente est une erreur."""
    full = full_sha(sha) if FULL_SHA.fullmatch(str(sha or "").strip().lower()) \
        else resolve_commit(sha, cwd=cwd)
    if full is None:
        raise ProvenanceError("commit %r does not resolve to a full commit" % (sha,))
    head = resolve_commit("HEAD", cwd=cwd)
    if head is None or head != full:
        raise ProvenanceError(
            "only the current worktree HEAD can be recorded (HEAD is %s, not %s)"
            % ((head or "?")[:12], full[:12]))
    from .identity import GIT_EMAIL_DOMAIN
    committer = subprocess.run(
        ["git", "log", "-1", "--format=%ce", full], capture_output=True, text=True,
        cwd=cwd or os.getcwd(), timeout=20)
    email = (committer.stdout or "").strip().lower()
    expected = ("%s@%s" % (identity["actor"], GIT_EMAIL_DOMAIN)).lower()
    if committer.returncode != 0 or email != expected:
        raise ProvenanceError(
            "commit %s was not committed by %s (committer %r)"
            % (full[:12], identity["actor"], email or "?"))
    subject = subject_of_commit(full)
    ops = storage.of(db).provenance
    existing = ops.get(subject)
    if existing is not None:
        # La preuve est déjà écrite : rejouer la même identité ne réécrit rien
        # (l'attestation a été consommée par la première écriture).
        if not _same_identity(existing, identity):
            raise ProvenanceError(
                "%s: already recorded for %s/%s, not %s/%s"
                % (subject, existing["actor"], existing["turn"],
                   identity["actor"], identity["turn"]))
        return dict(existing, recorded=False)
    intent = read_commit_intent(cwd)
    if intent is None:
        raise ProvenanceError(
            "no creation attestation for %s: only a commit created in this turn"
            " can be recorded" % full[:12])
    if (intent["actor"] != identity["actor"] or intent["turn"] != identity["turn"]
            or intent["harness"] != identity["harness"]):
        raise ProvenanceError(
            "creation attestation of %s/%s/%s is not from this turn (%s/%s/%s)"
            % (intent["actor"], intent["turn"][:8], intent["harness"],
               identity["actor"], identity["turn"][:8], identity["harness"]))
    if not _attests_creation(cwd, full, intent):
        raise ProvenanceError(
            "commit %s is not the creation attested for this turn" % full[:12])
    row = ops.add(subject, full, identity["actor"], identity["session"],
                  identity["turn"], identity["harness"], work_item_id=work_item_id)
    if not row["recorded"] and not _same_identity(row, identity):
        raise ProvenanceError(
            "%s: already recorded for %s/%s, not %s/%s"
            % (subject, row["actor"], row["turn"], identity["actor"], identity["turn"]))
    clear_commit_intent(cwd)
    return row


#: les hooks posés par `ameesh provenance install` — ils ne bloquent JAMAIS un
#: commit : hors tour managé, sans base ou sans ameesh, ils sortent 0.
#: `pre-commit` atteste la création (identité du tour, HEAD et arbre de départ),
#: `post-commit` écrit la preuve en consommant l'attestation.
PRE_COMMIT_HOOK = """#!/bin/sh
# ameesh #1465 — attestation de creation du commit, avant qu'il naisse.
# Ne bloque jamais un commit : hors tour manage ou sans ameesh, sort 0.
[ "${AMEESH_TURN_ID:-}" ] || exit 0
command -v ameesh >/dev/null 2>&1 || exit 0
ameesh provenance intent >/dev/null 2>&1 || exit 0
exit 0
"""

HOOK = """#!/bin/sh
# ameesh #1465 — preuve d'origine du commit, dans le tour qui l'a cree.
# Ne bloque jamais un commit : hors tour manage ou sans base, sort 0.
[ "${AMEESH_TURN_ID:-}" ] || exit 0
command -v ameesh >/dev/null 2>&1 || exit 0
ameesh provenance record --commit HEAD --quiet >/dev/null 2>&1 || exit 0
exit 0
"""


def _install_one(path: str, content: str) -> dict:
    """Pose un hook ; un hook ÉTRANGER n'est jamais écrasé."""
    name = os.path.basename(path)
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            current = fh.read()
        if current != content and "ameesh #1465" not in current:
            return {"path": path, "installed": False,
                    "reason": "a foreign %s hook is already installed" % name}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(path, 0o755)
    return {"path": path, "installed": True, "reason": "installed"}


def install_hook(repo: str) -> dict:
    """Pose la paire `pre-commit`/`post-commit` dans le dépôt `repo`.

    Idempotent : un hook absent ou déjà le nôtre est (ré)écrit ; un hook
    ÉTRANGER n'est jamais écrasé — on le signale, l'agent décide. `installed`
    n'est vrai que si les DEUX hooks sont les nôtres : une preuve exacte a
    besoin de l'attestation de création et de son écriture. Rend
    `{"path", "installed", "reason", "hooks"}`."""
    common = _git(repo, "rev-parse", "--git-common-dir")
    if not common:
        return {"path": "", "installed": False, "hooks": {},
                "reason": "not a git repository: %s" % (repo,)}
    if not os.path.isabs(common):
        common = os.path.join(repo, common)
    hooks = os.path.join(common, "hooks")
    out = {name: _install_one(os.path.join(hooks, name), content)
           for name, content in (("pre-commit", PRE_COMMIT_HOOK),
                                 ("post-commit", HOOK))}
    blocked = [name for name, row in out.items() if not row["installed"]]
    return {
        "path": out["post-commit"]["path"],
        "installed": not blocked,
        "hooks": out,
        "reason": "" if not blocked else "; ".join(
            "%s: %s" % (name, out[name]["reason"]) for name in blocked),
    }


def _connect():
    from . import config as config_mod, db as db_mod
    cfg = config_mod.load()
    db = db_mod.connect(cfg)
    db_mod.require_schema(db)
    return db


def build_parser():
    import argparse
    parser = argparse.ArgumentParser(
        prog="ameesh provenance",
        description="#1465 : preuves exactes d'origine d'un commit et d'un jalon.")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="record the exact origin of a commit")
    rec.add_argument("--commit", required=True, help="commit id (HEAD accepted)")
    rec.add_argument("--lot", type=int, default=None, help="attach to a work item")
    rec.add_argument("--quiet", action="store_true")
    rec.add_argument("--json", action="store_true")
    inst = sub.add_parser("install", help="install the provenance hooks")
    inst.add_argument("--repo", default=".", help="repository (default: cwd)")
    inst.add_argument("--json", action="store_true")
    att = sub.add_parser("intent",
                         help="write the creation attestation (pre-commit hook)")
    att.add_argument("--json", action="store_true")
    show = sub.add_parser("show", help="read the receipt, or nothing if unknown")
    show.add_argument("--commit", default="")
    show.add_argument("--milestone", type=int, default=None)
    show.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    import json
    import sys
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "install":
        out = install_hook(args.repo)
        if args.json:
            print(json.dumps(out))
        elif out["installed"]:
            print("post-commit hook installed: %s" % out["path"])
        else:
            print("post-commit hook NOT installed: %s" % out["reason"], file=sys.stderr)
        return 0 if out["installed"] else 1
    if args.command == "intent":
        # aucune base requise : le hook `pre-commit` atteste, `post-commit` écrit
        attestation = prepare_commit_intent()
        if args.json:
            print(json.dumps(attestation, sort_keys=True))
        return 0
    db = _connect()
    try:
        if args.command == "record":
            identity = capture()
            identity["session"] = live_session(db, identity["actor"],
                                               harness=identity["harness"])
            row = record_commit(db, args.commit, identity=identity,
                                work_item_id=args.lot)
            if args.json:
                print(json.dumps(row, default=str))
            elif not args.quiet:
                print("provenance recorded: %s by %s (turn %s)"
                      % (row["subject"], row["actor"], row["turn"][:8]))
            return 0
        if args.commit:
            recu = receipt_for_commit(db, args.commit)
        elif args.milestone is not None:
            recu = receipt_for_milestone(db, args.milestone)
        else:
            print("error: --commit or --milestone is required", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(recu, default=str))
        elif recu is None:
            print("unknown origin (no exact receipt)")
        else:
            print("%s: %s in %s, turn %s, %s"
                  % (recu["subject"], recu["actor"], recu["session"],
                     recu["turn"][:8], recu["record"]))
        return 0 if recu is not None else 3
    except ProvenanceError as exc:
        print("provenance refused: %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()
