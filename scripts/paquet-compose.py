#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Composition d'une unité de paquet avec le trailer `ameesh-work:` (#2157).

    scripts/paquet-compose.py --lot 2131 --source ef22692ba6a3 --cut 0400 \
        --note "détachement conditionnel CAS et PR détachée"

Fusionne `--source` dans la branche courante (`git merge --no-ff --no-commit`),
écrit le message de composition et y ajoute une ligne `ameesh-work: <lot>` par
lot porté (répéter `--lot` pour une unité qui en porte plusieurs). Le commit
devient déclarable nativement : `ameesh work merged --sha <commit de
composition>` l'accepte, et `work sync-branches` le voit (`cited_lots`,
`merge_citing`) même quand la remise du lot diffère de son gel enregistré
(#1628). Aucune autre modification de l'arbre : la fusion est celle du
composeur, le trailer ne change ni contenu ni CI.

Sur fusion propre, le commit est créé et son SHA affiché. Sur conflit, rien
n'est commité : le message complet (sujet, corps, trailer) est déjà écrit dans
`.git/MERGE_MSG`, il suffit de résoudre, `git add`, puis `git commit --no-edit`.

Le sujet par défaut nomme chaque lot avec la tête composée, `#<lot> <tête>`
(#2360) : c'est la forme d'unité que reconnaît `planned_waits` (#2335) ; un
`#N` seul reste une simple citation.

    scripts/paquet-compose.py --units --base origin/main [--head <réf>]

relit les unités de fusion déjà composées sur la branche du paquet (commits de
fusion de la première parente depuis `--base` qui portent `ameesh-work:`) et
imprime une ligne `N unités de fusion, M lots portés : #<lot> <tête>, …`
(tête = second parent, 12 caractères) à reprendre telle quelle dans le corps du
lot porteur et dans le manifeste (#2360) : le porteur déclare ainsi ses unités
sans rien recopier à la main. Une unité peut porter plusieurs lots, tous listés
avec leur tête. Une fusion sans trailer (mise à jour depuis la cible) n'est pas
une unité ; un lot recomposé garde sa place, prend la tête la plus récente et
son unité est le commit de composition le plus récent. Le compteur distingue
donc les unités de fusion encore rattachées à un lot des lots portés (#2387).

Ne réécrit aucun commit existant : la convention vaut pour les unités
composées à partir de maintenant (un paquet déjà validé n'est pas refait pour
ce seul métadonné).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

TRAILER = "ameesh-work"
TRAILER_RE = re.compile(r"(?im)^[ \t>*-]*ameesh-work[ \t]*:[ \t]*#?(\d+)[ \t]*$")

#: #2546 : les instructions de saut reconnues par GitHub (mêmes formes que
#: `ci.SKIP_CI_RE`). Un message de tête qui en porte une — même en citation —
#: ne démarre aucun workflow `push` ou `pull_request`, l'événement `labeled`
#: de la CI compris : le composeur ne doit jamais créer une telle tête.
SKIP_CI_RE = re.compile(
    r"\[[ \t]*(?:skip[ \t]+ci|ci[ \t]+skip|no[ \t]+ci|skip[ \t]+actions|"
    r"actions[ \t]+skip)[ \t]*\]", re.IGNORECASE)


class GitError(RuntimeError):
    """Une commande git a échoué, ou l'état du dépôt ne permet pas la fusion."""


class InputError(RuntimeError):
    """Un argument désigne une source inexistante ou déjà composée."""


def git(repo: Path, *args: str, check: bool = True,
        input_text: str | None = None) -> subprocess.CompletedProcess:
    """Lance `git -C repo …`, texte UTF-8, sans terminal interactif."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_text.encode("utf-8") if input_text is not None else None,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise GitError(
            "git %s: %s" % (" ".join(args),
                            proc.stderr.decode("utf-8", "replace").strip())
        )
    return proc


def out(repo: Path, *args: str, check: bool = True) -> str:
    return git(repo, *args, check=check).stdout.decode("utf-8", "replace").strip()


def rev(repo: Path, ref: str) -> str | None:
    """Le commit désigné par `ref`, ou None s'il n'existe pas."""
    proc = git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}",
               check=False)
    return proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else None


def git_path(repo: Path, name: str) -> Path:
    """Le chemin de `name` dans le répertoire git, worktree lié compris."""
    path = Path(out(repo, "rev-parse", "--git-path", name))
    return path if path.is_absolute() else repo / path


def build_message(lots: list[int], short_source: str, cut: str | None,
                  note: str | None, subject: str | None,
                  bodies: list[str]) -> str:
    """Le message de composition : sujet, corps, puis un trailer par lot."""
    if subject:
        title = subject.strip()
    else:
        # `#<lot> <tête>` : la forme d'unité des porteurs (#2335, #2360)
        refs = ", ".join("#%d %s" % (lot, short_source) for lot in lots)
        if cut:
            title = "vehicule %s : %s" % (cut.strip(), refs)
        else:
            title = "composition : %s" % refs
        if note:
            title += " — " + note.strip()
    paragraphs = [title]
    paragraphs.extend(body.strip() for body in bodies if body.strip())
    text = "\n\n".join(paragraphs)
    # un lot déjà cité dans le corps n'est pas répété en trailer
    already = {int(match.group(1)) for match in TRAILER_RE.finditer(text)}
    lines = ["%s: %d" % (TRAILER, lot) for lot in lots if lot not in already]
    return text + "\n\n" + "\n".join(lines) + "\n" if lines else text + "\n"


def reject_skip_marker(label: str, text: str | None) -> None:
    """Refuse `text` s'il porte une instruction de saut GitHub (#2546)."""
    match = SKIP_CI_RE.search(text or "")
    if not match:
        return
    raise InputError(
        "%s porte l'instruction de saut GitHub « %s » : la tête composée ne "
        "démarrerait aucun workflow, pas même l'événement pull_request: labeled "
        "de sa CI. Retirez ce marqueur du message et recomposez ; une tête qui "
        "doit réellement sauter la CI ne passe pas par cet outil (les commits de "
        "release [skip ci] restent hors de lui)." % (label, match.group(0)))


def check_repo(repo: Path) -> Path:
    """La racine du worktree, après vérification que `repo` en est un."""
    inside = out(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if inside != "true":
        raise GitError("%s n'est pas un worktree git" % repo)
    return Path(out(repo, "rev-parse", "--show-toplevel"))


def packet_units(repo: Path, base: str, head: str = "HEAD"
                 ) -> tuple[dict[int, str], int]:
    """`({lot: tête}, unités de fusion)` des compositions de `base` à `head`,
    dans l'ordre de composition (#2360, #2387).

    Une unité de fusion est un commit de fusion de la première parente qui
    porte `ameesh-work:` ; sa tête est le second parent (la source composée).
    Une unité peut porter plusieurs lots. Un lot recomposé garde sa place,
    prend la tête la plus récente, et son unité est le commit de composition le
    plus récent : seules les unités encore rattachées à au moins un lot sont
    comptées.
    """
    if rev(repo, base) is None:
        raise InputError("base %r introuvable (SHA ou réf)" % base)
    if rev(repo, head) is None:
        raise InputError("tête %r introuvable (SHA ou réf)" % head)
    log = out(repo, "log", "--first-parent", "--merges", "--reverse",
              "--format=%H%x1f%P%x1f%B%x1e", "%s..%s" % (base, head))
    units: dict[int, str] = {}
    unit_of: dict[int, str] = {}
    for record in log.split("\x1e"):
        commit, _, rest = record.strip().partition("\x1f")
        parents, _, body = rest.partition("\x1f")
        parents = parents.split()
        if len(parents) < 2:
            continue
        source_head = out(repo, "rev-parse", "--short=12", parents[1])
        for match in TRAILER_RE.finditer(body):
            lot = int(match.group(1))
            units[lot] = source_head
            unit_of[lot] = commit
    return units, len(set(unit_of.values()))


def list_units(args: argparse.Namespace) -> int:
    repo = check_repo(Path(args.repo).resolve())
    units, merges = packet_units(repo, args.base, args.head)
    if not units:
        raise InputError("aucune unité `ameesh-work:` depuis %s" % args.base)
    print("%d unités de fusion, %d lots portés : %s" % (merges, len(units), ", ".join(
        "#%d %s" % (lot, head) for lot, head in units.items())))
    return 0


def compose(args: argparse.Namespace) -> int:
    if args.units:
        if args.lot or args.source:
            raise InputError("--units : ni --lot ni --source")
        if not args.base:
            raise InputError("--units : --base (la cible du paquet) requis")
        return list_units(args)
    if not args.source:
        raise InputError("--source : la tête à composer")
    lots = list(dict.fromkeys(args.lot or ()))
    if not lots:
        raise InputError("--lot: au moins un lot porté")
    if any(lot <= 0 for lot in lots):
        raise InputError("--lot: des identifiants de lot positifs")
    repo = check_repo(Path(args.repo).resolve())
    if rev(repo, "MERGE_HEAD"):
        raise InputError("une fusion est déjà en cours : terminer ou annuler "
                         "(`git merge --abort`) avant de composer")
    head = rev(repo, "HEAD")
    if head is None:
        raise InputError("HEAD n'a aucun commit : la branche du paquet est vide")
    source = rev(repo, args.source)
    if source is None:
        raise InputError("source %r introuvable (SHA ou réf)" % args.source)
    if source == head:
        raise InputError("source %s = HEAD : rien à composer" % source[:12])
    if git(repo, "merge-base", "--is-ancestor", source, head,
           check=False).returncode == 0:
        raise InputError("source %s est déjà ancêtre de HEAD : rien à composer"
                         % source[:12])
    short_source = out(repo, "rev-parse", "--short=12", source)
    message = build_message(lots, short_source, args.cut, args.note,
                            args.subject, args.body)
    # #2546 : refuser avant la fusion, sans toucher à l'arbre ni au message.
    # Chaque champ est contrôlé pour lui-même (`--note` est ignoré quand
    # `--subject` est donné), puis le message final, qui porte le sujet par
    # défaut et le trailer.
    reject_skip_marker("--subject", args.subject)
    reject_skip_marker("--note", args.note)
    reject_skip_marker("--cut", args.cut)
    for body in args.body:
        reject_skip_marker("--body", body)
    reject_skip_marker("le message composé", message)

    proc = git(repo, "merge", "--no-ff", "--no-commit", "--no-edit", source,
               check=False)
    if proc.returncode == 0:
        done = git(repo, "commit", "-F", "-", input_text=message, check=False)
        if done.returncode != 0:
            raise GitError(
                "git commit: %s" % done.stderr.decode("utf-8", "replace").strip()
            )
        sha = out(repo, "rev-parse", "HEAD")
        print("unité composée : %s" % sha)
        print("lots portés : %s" % ", ".join("#%d" % lot for lot in lots))
        return 0

    if rev(repo, "MERGE_HEAD"):
        # conflit : le message est prêt, le composeur résout et commite
        message_path = git_path(repo, "MERGE_MSG")
        message_path.write_text(message, encoding="utf-8")
        conflicts = out(repo, "diff", "--name-only", "--diff-filter=U",
                        check=False).splitlines()
        print("conflit de composition : aucun commit créé", file=sys.stderr)
        for name in conflicts:
            print("  conflit : %s" % name, file=sys.stderr)
        print("message préparé dans %s (trailer compris) ; résoudre, "
              "`git add`, puis `git commit --no-edit`." % message_path,
              file=sys.stderr)
        return 1

    raise GitError("git merge: %s"
                   % proc.stderr.decode("utf-8", "replace").strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compose une unité de paquet avec un trailer ameesh-work: "
                    "par lot porté (#2157)."
    )
    parser.add_argument("--repo", default=".", help="worktree du paquet (défaut : .)")
    parser.add_argument("--lot", type=int, action="append", default=None,
                        help="lot porté par l'unité (répétable)")
    parser.add_argument("--source", default=None,
                        help="tête, remise ou résolution à composer (SHA ou réf)")
    parser.add_argument("--units", action="store_true",
                        help="imprimer les unités de fusion et les lots "
                             "« #<lot> <tête> » composés depuis --base, pour "
                             "le porteur (#2360, #2387)")
    parser.add_argument("--base", default=None,
                        help="avec --units : la cible dont part le paquet")
    parser.add_argument("--head", default="HEAD",
                        help="avec --units : la tête du paquet (défaut : HEAD)")
    parser.add_argument("--cut", default=None,
                        help="étiquette de coupure du sujet (ex. 0400)")
    parser.add_argument("--note", default=None,
                        help="courte description, après le sujet")
    parser.add_argument("--subject", default=None,
                        help="sujet complet, à la place du sujet par défaut")
    parser.add_argument("--body", action="append", default=[],
                        help="paragraphe de corps (répétable)")
    args = parser.parse_args(argv)

    try:
        return compose(args)
    except (GitError, InputError) as error:
        print("paquet-compose : %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
