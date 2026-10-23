# SPDX-License-Identifier: AGPL-3.0-only
"""Une seule lecture des auteurs d'une tête gelée (#2681).

La porte de fusion (`merge_queue.review_gate`) compare les avis à tous les
auteurs que l'appelant lui donne (#2681) ; la désignation au gel
(`review_designation_activation`) et le train (#1467) doivent lire les mêmes
auteurs, avec les mêmes exceptions du canon §2 :

* le titulaire de référence est l'assigné au gel
  (`review_lot._author_at_freeze`, la même trace historique que la porte) ;
  il n'est **auteur que s'il a écrit un commit de la plage gelée** (#2808) :
  auteur Git du commit, ou committer nommé par le reçu exact / le journal des
  tours. Sa famille n'est interdite qu'avec cette preuve d'écriture. Sur un lot
  porteur de paquet (`integration/**`), l'assigné est le composeur mécanique,
  pas un auteur (#2803), et le commit de manifeste est écarté de la même
  lecture ;
* chaque commit du contenu est attribué par son reçu exact (#1465) ;
* un correctif de premier tour d'un **relecteur désigné**, même sous le seuil
  du §2 (`REVIEWER_FIX_LINES`, pas de conception), fait de son auteur un
  auteur de la tête (#2888) : sa famille n'est plus éligible. Le seuil ne
  décide que de la recevabilité process (une correction trop grosse revient à
  l'auteur), jamais de l'indépendance. Le correcteur reste tenu hors des
  relecteurs par son nom (#2739) ;
* une réécriture d'historique **à contenu identique** n'efface pas la
  provenance (#2924) : les auteurs des gels antérieurs du même lot dont le
  contenu subsiste restent auteurs de la tête. Une consolidation sur une cible
  avancée, un squash à arbre identique ou le report des lignes significatives
  d'un correctif gardent donc sa famille exclue ; un contenu réellement réécrit
  ne la reprend pas, et le doute conserve toujours l'auteur ;
* un commit **déjà dans la cible** au moment de la lecture ne compte ni comme
  auteur ni comme tiers du lot (#3116) : la plage reportée d'un ancien gel,
  calculée avant un rebase, peut contenir du develop déjà relu sous son propre
  lot ; un inconnu d'un gel antérieur qui n'est plus ancêtre de la tête et dont
  le contenu ne survit pas ne bloque pas non plus (#3116) ;
* quand le reçu exact manque (commit restauré par rebase, correction poussée
  hors du hook), le commit est attribué à la famille de session de son
  committer, lue au journal des tours (`turn_costs`, `_turns_harness`) : un
  commit du relecteur désigné reste sa correction, un commit de l'assigné
  reste l'auteur. Un tiers non relecteur sans reçu reste inconnu : aucun camp
  ne le suppose. #2721 : à la désignation, ce repli porte sa trace
  (`proof: turn-journal`, `fallback_commits`), et un committer distinct de la
  référence doit être au registre.

Le module ne devine jamais une famille depuis un nom Git seul : le committer
doit porter l'adresse d'agent (`<nom>@ameesh.invalid`) et la famille vient du
journal des tours. Un reçu exact contradictoire est une erreur, pas une
absence.
"""
from __future__ import annotations

from typing import Mapping, Optional, Sequence

from . import plan_git, registry, review_lot, review_provenance, storage

#: canon §2 : une correction de premier tour va jusqu'à ~50 lignes changées
REVIEWER_FIX_LINES = 50
#: canon §2 « ou de conception » : un document de conception n'est jamais petit
DESIGN_PREFIXES = ("docs/design/",)
#: le manifeste d'un paquet (`reviews/integration-<nom>.md`) : la composition
#: mécanique du composeur, jamais le contenu relu (#2803)
PACKET_MANIFEST_PREFIX = "reviews/integration-"
PACKET_MANIFEST_SUFFIX = ".md"


def packet_composer(item: Mapping) -> str:
    """Le composeur d'un lot **porteur de paquet** (`integration/**`), sinon "".

    Un lot dont la branche est `integration/**` est le véhicule d'un paquet :
    son assigné est le composeur, dont le geste est mécanique (canon §2 et
    §11 bis). Ce n'est pas un auteur du contenu relu (#2803) : les auteurs à
    exclure sont ceux des lots du manifeste, lus sur la plage gelée. Le
    composeur reste un auteur s'il a réellement écrit un commit de contenu
    (le reçu de ce commit le nomme) ; seule sa place de référence tombe.
    """
    from . import packet_guard

    item = item or {}
    if not packet_guard.is_packet_branch(item.get("branch")):
        return ""
    return str(item.get("assignee") or "").strip()


def _manifest_commit(repo: str, sha: str) -> bool:
    """Le commit n'écrit que le manifeste d'un paquet (`reviews/integration-*.md`).

    La composition mécanique d'un paquet (#2803) : le composeur y déclare les
    unités, il n'écrit pas le contenu relu. Un diff illisible ne l'écarte
    jamais : le doute garde l'auteur (l'indépendance ne se suppose pas).
    """
    if not repo:
        return False
    text = plan_git._out(repo, "show", "--name-only", "--format=", sha)
    if text is None:
        return False
    paths = [line.strip() for line in str(text).splitlines() if line.strip()]
    return bool(paths) and all(
        path.startswith(PACKET_MANIFEST_PREFIX)
        and path.endswith(PACKET_MANIFEST_SUFFIX) for path in paths)


def _family(harness, families) -> str:
    return review_lot.family_of(str(harness or ""), families)


def _fields(row) -> dict:
    """Un reçu (dataclass ou dict) en champs, sans compléter aucun champ."""
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return dict(row)
    if hasattr(row, "__dict__"):
        return dict(vars(row))
    return {}


def _commit_receipt(provider, db, sha: str, *, strict: bool) -> Optional[dict]:
    """Le reçu exact du commit, ou None ; une preuve corrompue lève si strict."""
    provider = provider or review_provenance
    error_type = getattr(provider, "ProvenanceError", review_provenance.ProvenanceError)
    try:
        row = provider.receipt_for_commit(db, sha)
    except error_type as exc:
        # Une preuve contradictoire n'est jamais une absence : le train refuse,
        # la désignation tient le commit pour inconnu (jamais deviné).
        raise review_provenance.ProvenanceError(
            "contradictory historical provenance for commit %s: %s"
            % (str(sha)[:12], exc)) from exc
    fields = _fields(row)
    if not fields:
        return None
    if strict:
        required = ("subject", "commit", "actor", "session", "turn", "harness", "record")
        if any(not isinstance(fields.get(key), str) or not fields.get(key).strip()
               for key in required):
            raise review_provenance.ProvenanceError(
                "malformed historical provenance for commit %s" % str(sha)[:12])
        if fields["subject"] != "commit:" + sha:
            raise review_provenance.ProvenanceError(
                "historical provenance names another subject: commit:%s" % str(sha)[:12])
        if fields["commit"] != sha:
            raise review_provenance.ProvenanceError(
                "historical provenance resolves another commit for commit:%s"
                % str(sha)[:12])
    return fields


def _milestone_receipt(provider, db, milestone_id: int, *, strict: bool) -> Optional[dict]:
    provider = provider or review_provenance
    error_type = getattr(provider, "ProvenanceError", review_provenance.ProvenanceError)
    try:
        row = provider.receipt_for_milestone(db, int(milestone_id))
    except error_type as exc:
        if strict:
            raise review_provenance.ProvenanceError(
                "contradictory historical provenance for milestone:%s: %s"
                % (milestone_id, exc)) from exc
        return None
    fields = _fields(row)
    if not fields:
        return None
    if strict:
        required = ("subject", "commit", "actor", "session", "turn", "harness", "record")
        if any(not isinstance(fields.get(key), str) or not fields.get(key).strip()
               for key in required):
            raise review_provenance.ProvenanceError(
                "malformed historical provenance for milestone:%s" % milestone_id)
        if fields["subject"] != "milestone:%s" % int(milestone_id):
            raise review_provenance.ProvenanceError(
                "historical provenance names another subject: milestone:%s"
                % milestone_id)
    return fields


def reference_author(db, item: Mapping, milestone: Optional[Mapping], families,
                     *, provider=None, strict: bool = False,
                     families_out: Optional[dict] = None) -> dict:
    """L'auteur de référence `{name, family}` : l'assigné au gel.

    La famille vient de la trace du gel (`_author_at_freeze`), sinon du reçu du
    jalon quand son acteur est bien l'auteur, sinon de la session ouverte du
    registre. C'est exactement la référence de la porte (`assignee`) ; le
    registre courant ne prouve une famille que lorsque le gel ne la donne pas.

    Le reçu du jalon est **toujours** lu quand il existe (#2961 B2), même si le
    journal des tours a déjà donné une famille : un reçu contradictoire doit
    être vu, et deux familles prouvées différentes sont une ambiguïté, pas un
    choix. La famille rendue n'est renseignée que si une seule famille est
    prouvée ; `families_out` (indexé par nom en minuscules) reçoit l'ensemble
    des preuves pour que la porte distingue une absence d'une ambiguïté.

    `strict=True` (le train, la porte) refuse un reçu de jalon contradictoire
    ou malformé (`ProvenanceError`) au lieu de lire son absence et de retomber
    sur le registre.

    #2803 : sur un lot porteur de paquet (`integration/**`), l'assigné est le
    **composeur** — un geste mécanique (canon §2/§11 bis), pas un auteur du
    contenu relu. La référence est vide : les auteurs sont ceux des lots du
    manifeste, lus sur les commits de la plage gelée.
    """
    item = dict(item or {})
    if packet_composer(item):
        return {"name": "", "family": ""}
    lot = int(item.get("id") or 0)
    assignee = str(item.get("assignee") or "").strip()
    author = {}
    try:
        author = review_lot._author_at_freeze(db, lot, item) or {}
    except Exception:  # noqa: BLE001 - trace illisible : famille inconnue
        author = {}
    name = str(author.get("name") or assignee).strip()
    proven: set[str] = set()

    def prove(harness) -> None:
        family = _family(harness, families)
        if family:
            proven.add(family)

    prove(author.get("harness"))
    # Un jalon sans identifiant positif n'est pas un reçu lisible : ce n'est
    # pas une preuve contradictoire, seulement une trace absente (#2961).
    try:
        milestone_id = int((milestone or {}).get("id") or 0)
    except (TypeError, ValueError):
        milestone_id = 0
    if milestone is not None and milestone_id > 0:
        try:
            proof = _milestone_receipt(provider, db, milestone_id,
                                       strict=strict)
        except review_provenance.ProvenanceError:
            # #2961 B2 : en mode strict (train, porte), un reçu contradictoire
            # n'est pas une absence — même quand le journal des tours a déjà
            # donné une famille. La preuve du gel ne se remplace pas.
            if strict:
                raise
            proof = None
        except (KeyError, TypeError, ValueError) as exc:
            if strict:
                raise review_provenance.ProvenanceError(
                    "malformed freeze milestone for the reference author") from exc
            proof = None
        if proof and str(proof.get("actor") or "").strip() == name:
            prove(proof.get("harness"))
    if not proven and name and (milestone is None
                                or str(milestone.get("actor") or "").strip()
                                in ("", name)):
        try:
            row = registry.get(db, name) or {}
            # le harnais déclaré n'est pas une session : seule une session
            # ouverte prouve la famille qui a réellement tourné (L242b, #1037)
            if row.get("session_id") or row.get("session_harness"):
                prove(row.get("session_harness"))
        except Exception:  # noqa: BLE001 - pas de session : famille inconnue
            pass
    if families_out is not None:
        families_out[name.lower()] = set(proven)
    family = next(iter(proven)) if len(proven) == 1 else ""
    return {"name": name, "family": family}


def designated_reviewers(db, item: Mapping, milestone: Optional[Mapping],
                         author: str) -> set[str]:
    """Les relecteurs désignés du lot, automatiques ou manuels (#2638).

    La colonne courante porte les noms du gel précédent et l'exception
    manuelle ; les marques du journal gardent les noms qu'un gel ultérieur a
    retirés. Un relecteur-correcteur doit rester reconnaissable après le gel
    qui l'a retiré. L'auteur du lot n'est jamais relecteur.
    """
    try:
        from .review_designation_activation import _designated_reviewers
        return _designated_reviewers(db, item, milestone, author)
    except Exception:  # noqa: BLE001 - journal ou colonne illisible : aucun nom
        return set()


def change(repo: str, sha: str) -> Optional[tuple[int, bool]]:
    """`(lignes changées, touche une conception)` d'un commit, ou None.

    None signifie que le diff n'est pas lisible : aucun petit correctif n'est
    supposé. Un binaire n'est jamais un petit correctif, ni un document de
    conception (canon §2 : « > ~50 lignes ou de conception » revient à
    l'auteur, #2638)."""
    if not repo:
        return None
    text = plan_git._out(repo, "show", "--numstat", "-z", "--format=",
                         "--no-renames", sha)
    if text is None:
        return None
    lines, design = 0, False
    for row in str(text).split("\0"):
        if not row:
            continue
        fields = row.split("\t", 2)
        if len(fields) < 3:
            return None
        added, removed, path = fields
        if added == "-" or removed == "-":
            return REVIEWER_FIX_LINES + 1, design
        try:
            lines += int(added) + int(removed)
        except ValueError:
            return None
        if path.startswith(DESIGN_PREFIXES):
            design = True
    return lines, design


def commit_actor(repo: str, sha: str) -> tuple[str, float]:
    """`(acteur, instant)` du committer d'adresse d'agent, sinon ("", 0).

    Le committer est l'agent qui a créé l'objet (un rebase le réécrit) ; le nom
    Git seul ne prouve rien, l'adresse `<nom>@ameesh.invalid` est celle du
    runner. L'instant sert au journal des tours, jamais à une proximité
    arbitraire."""
    text = plan_git._out(repo, "show", "-s", "--format=%ce%x1f%ct", sha)
    if text is None:
        return "", 0.0
    fields = str(text).strip().split("\x1f")
    if len(fields) != 2:
        return "", 0.0
    email, raw_ts = fields[0].strip().lower(), fields[1].strip()
    try:
        stamp = float(raw_ts)
    except ValueError:
        return "", 0.0
    from .identity import GIT_EMAIL_DOMAIN
    suffix = "@" + GIT_EMAIL_DOMAIN
    if not email.endswith(suffix):
        return "", 0.0
    return email[:-len(suffix)], stamp


def commits_written_by(repo: str, commits: Sequence[str], name: str) -> Optional[bool]:
    """Un commit de la plage porte `name` comme **auteur Git** (`%ae`).

    L'auteur Git est la signature du contenu ; le committer (`commit_actor`)
    est l'agent qui a créé l'objet. Un amend ou un rebase conserve l'auteur
    d'origine et change le committer : les deux ont écrit la plage (#2808).
    Le nom Git seul ne prouve rien : l'adresse `<nom>@ameesh.invalid` est
    exigée, comme pour le committer."""
    if not repo or not name:
        return False
    shas = [str(sha).strip() for sha in commits or () if str(sha).strip()]
    if not shas:
        return False
    text = plan_git._out(repo, "log", "--no-walk", "--format=%H%x1f%ae", *shas)
    if text is None or len(str(text).splitlines()) != len(set(shas)):
        return None                     # unreadable is not proof of absence
    from .identity import GIT_EMAIL_DOMAIN
    suffix = "@" + GIT_EMAIL_DOMAIN
    wanted = str(name).strip().lower()
    for line in str(text).splitlines():
        _sha, separator, email = line.partition("\x1f")
        if not separator:
            return None
        email = email.strip().lower()
        if email.endswith(suffix) and email[:-len(suffix)] == wanted:
            return True
    return False


def actor_family(db, actor: str, at_ts: float, families) -> str:
    """La famille de session d'un acteur à l'instant d'un fait, ou "".

    `turn_costs` est la seule trace du harnais qui a réellement tourné ; une
    ligne écrite à la fin du tour couvre le commit qui vient d'être créé
    (`contains=True`, la fenêtre de `review_lot._turns_harness`)."""
    if not actor or not at_ts:
        return ""
    try:
        harness = review_lot._turns_harness(db, actor, float(at_ts))
    except Exception:  # noqa: BLE001 - journal illisible : famille inconnue
        return ""
    return _family(harness, families)


# --------------------------------------------------------------------------
# #2924 : les auteurs des gels antérieurs du lot
# --------------------------------------------------------------------------
#
# Une réécriture d'historique à contenu identique (squash, consolidation sur une
# cible avancée) fait disparaître les commits qui portaient les reçus : le
# lecteur ne voit plus que le commit réécrit, et les correcteurs redeviennent
# « indépendants ». Le contenu, lui, n'a pas changé : ses auteurs restent
# auteurs (canon §2, provenance réelle). On relit donc les gels antérieurs du
# même lot et l'on reprend leurs auteurs **tant que leur contenu subsiste** :
#
# * gel antérieur ancêtre de la tête lue et absent de la base : son contenu est
#   dans l'histoire, donc conservé ;
# * arbre de la tête égal à celui du gel, ou à la fusion de ce gel avec la
#   base : contenu identique, conservé ;
# * sinon le contenu a été réécrit : un commit antérieur n'est tenu pour
#   réécrit que si **aucune** de ses lignes significatives ne subsiste. Une
#   ligne ou un commentaire modifié ne suffit donc jamais à retirer l'auteur ;
#   une réécriture complète ne le reprend pas. Le doute (lecture illisible ou
#   ambiguë) conserve toujours l'auteur.
#
# Le doute conserve toujours l'auteur : la porte ne doit jamais rendre une
# famille « indépendante » par accident. Un correctif réellement réécrit par
# l'auteur n'est donc plus repris, et une décision documentée reste possible
# hors du lecteur. Un lot porteur de paquet (`integration/**`) est exclu : sa
# porte relit le delta propre du véhicule (#2803), pas le contenu des membres
# déjà relus sur leur propre tête.

#: longueur minimale d'une ligne « significative » pour la preuve de contenu
MIN_CONTENT_LINE = 8
#: lignes de code génériques : leur présence ne prouve aucun contenu propre
_TRIVIAL_LINES = frozenset((
    "pass", "return", "return true", "return false", "break", "continue",
    "else:", "try:", "finally:", "except:", "}", ")", "]", "end",
))


def _is_ancestor(repo: str, before: str, after: str) -> Optional[bool]:
    """`before` est-il ancêtre de `after` ? None si Git ne peut pas le dire."""
    if not repo or not before or not after:
        return None
    proc = plan_git._git(repo, "merge-base", "--is-ancestor", before, after)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def target_commit(repo: str, target: str) -> str:
    """Le commit qu'une cible nomme dans `repo`, ou "" (#3116).

    La cible peut être un commit complet ou une ref (`origin/develop`) : c'est
    la cible **au moment de la lecture**. Une cible illisible rend "" et le
    lecteur garde son comportement d'avant (aucun commit n'est écarté) : le
    doute ne retire jamais un auteur.

    #3116 B2 : une cible NUE (`main`) se résout sur sa ref **distante**
    (`origin/main`) en priorité — même règle que `_resolve_base` et
    `_delta_base`. Une branche locale peut porter un commit non fusionné (le
    correcteur) et masquer la cible réelle ; la lire écarterait ce correcteur
    de la provenance.

    #3116 B3 : cette ref distante se lit **pleinement qualifiée**
    (`refs/remotes/origin/main`) ; le nom court `origin/main` peut lui-même
    être masqué par une branche ou un tag local homonyme. Une cible déjà
    `refs/...` se lit exactement, sans détour."""
    text = str(target or "").strip()
    if not repo or not text:
        return ""
    remote = None
    try:
        from . import canon as canon_mod
        remote = canon_mod.remote_branch(repo, text)
    except Exception:  # noqa: BLE001 - résolution distante impossible : ref nue
        remote = None
    if text.startswith("refs/"):
        refs = [text]
    else:
        if remote:
            remote = "refs/remotes/" + remote
        refs = ([remote] if remote else []) + [text]
    for ref in refs:
        try:
            found = plan_git._commit(repo, ref) or ""
        except Exception:  # noqa: BLE001 - cible illisible : pas d'écart
            found = ""
        if found:
            return found
    return ""


def _not_in_target(repo: str, commits, target: str) -> list:
    """Les commits de la liste qui ne sont PAS atteignables depuis `target`.

    #3116 : un commit déjà dans la cible a été relu sous son propre lot ; il
    ne compte ni comme auteur ni comme tiers du lot, qu'il vienne de la plage
    courante ou du report d'un gel antérieur (#2924). La lecture se fait en un
    seul `git rev-list ... --not <cible>`, sans une sonde par commit ; une
    lecture impossible garde la liste entière (le doute ne retire jamais un
    auteur, il ne le compte pas non plus dans la cible)."""
    wanted = [str(commit).strip() for commit in commits or ()
              if str(commit).strip()]
    if not wanted or not target:
        return wanted
    try:
        text = plan_git._out(repo, "rev-list", *wanted, "--not", str(target))
    except Exception:  # noqa: BLE001 - lecture impossible : aucun écart
        return wanted
    if text is None:
        return wanted
    outside = {line.strip() for line in str(text).splitlines() if line.strip()}
    return [commit for commit in wanted if commit in outside]


def _survives_rewrite(repo: str, head: str, sha: str, cache: dict,
                      base: str = "", target: str = "") -> bool:
    """Un commit d'un gel antérieur compte-t-il encore pour la tête lue ?

    #3116 : un commit déjà dans la cible ne compte ni comme auteur ni comme
    tiers. Hors cible, un commit qui n'est plus ancêtre de la tête **et** dont
    le contenu ne survit pas (preuve de contenu #2924/#2977) a été réellement
    réécrit : il ne bloque pas la lecture. Un commit encore ancêtre de la tête
    reste lu comme avant ; une lecture impossible est un doute, qui garde."""
    if not head:
        return True
    if target and _is_ancestor(repo, sha, target) is True:
        return False
    if _is_ancestor(repo, sha, head) is True:
        return True
    # #3116 B1 : la preuve de réécriture cherche le contenu DÉPLACÉ (un
    # fichier renommé garde ses lignes) et garde le doute quand un chemin a
    # disparu — les drapeaux stricts réservés aux lignes génériques faisaient
    # disparaître un inconnu dont le contenu subsistait dans un autre fichier.
    return _commit_survives(repo, head, sha, cache, base)


def _tree_of(repo: str, rev: str) -> Optional[str]:
    """L'arbre d'un commit, ou None s'il est illisible."""
    if not repo or not rev:
        return None
    return plan_git._out(repo, "rev-parse", str(rev) + "^{tree}")


def _merged_tree(repo: str, left: str, right: str) -> Optional[str]:
    """L'arbre de la fusion `left`+`right`, ou None (conflit ou illisible)."""
    if not repo or not left or not right:
        return None
    proc = plan_git._git(repo, "merge-tree", "--write-tree", left, right)
    if proc.returncode != 0:
        return None
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    return lines[0].strip() if lines else None


def _commits_between(repo: str, start: str, head: str) -> Optional[list]:
    """Les commits de contenu de `start..head`, ou None si illisible."""
    if not repo or not start or not head:
        return None
    text = plan_git._out(repo, "rev-list", "--no-merges", "%s..%s" % (start, head))
    if text is None:
        return None
    return [line.strip() for line in str(text).splitlines() if line.strip()]


def _commit_lines(repo: str, sha: str):
    """`({chemin: [lignes ajoutées]}, {chemin: [lignes retirées]}, binaire)`.

    Le diff est lu hunk par hunk : un en-tête `+++ b/` ou `--- a/` n'est jamais
    confondu avec une ligne de contenu qui commencerait par `++`/`--`. Un diff
    binaire ne se compare pas ligne à ligne : le lecteur conserve l'auteur
    (jamais l'inverse)."""
    text = plan_git._out(repo, "show", "--format=", "--unified=0",
                         "--no-renames", sha)
    if text is None:
        return None
    adds: dict = {}
    removes: dict = {}
    path, old_path, binary, hunk = "", "", False, False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            # un nouveau fichier : les en-têtes suivants sont de vrais en-têtes
            path, old_path, hunk = "", "", False
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            binary = True
            continue
        if not hunk:
            if line.startswith("@@"):
                hunk = True
            elif line.startswith("--- a/"):
                old_path = line[len("--- a/"):]
            elif line.startswith("--- /dev/null"):
                old_path = ""
            elif line.startswith("+++ b/"):
                path = line[len("+++ b/"):]
            elif line.startswith("+++ /dev/null"):
                path = old_path            # fichier supprimé
            continue
        if line.startswith("@@"):
            continue
        if not path:
            continue
        if line.startswith("+"):
            adds.setdefault(path, []).append(line[1:])
        elif line.startswith("-"):
            removes.setdefault(path, []).append(line[1:])
    return adds, removes, binary


def _meaningful_line(text: str) -> bool:
    """Une ligne assez propre pour prouver un contenu (pas un `}` ou un `pass`)."""
    value = str(text or "").strip()
    if len(value) < MIN_CONTENT_LINE or not any(ch.isalpha() for ch in value):
        return False
    return value.lower() not in _TRIVIAL_LINES


def _head_lines(repo: str, head: str, path: str, cache: dict):
    """Les lignes (dépouillées) de `path` dans la tête, ou None si absentes."""
    if path not in cache:
        text = plan_git._out(repo, "show", "%s:%s" % (head, path))
        cache[path] = None if text is None else {
            line.strip() for line in str(text).splitlines()}
    return cache[path]


def _path_line_counts(repo: str, rev: str, path: str, cache: dict):
    """Le nombre d'occurrences de chaque ligne (dépouillée) de `path` dans
    `rev`, ou None si le chemin y est absent ou illisible (#2977 B2)."""
    key = ("counts", rev, path)
    if key not in cache:
        text = plan_git._out(repo, "show", "%s:%s" % (rev, path))
        counts = None
        if text is not None:
            counts = {}
            for line in str(text).splitlines():
                counts[line.strip()] = counts.get(line.strip(), 0) + 1
        cache[key] = counts
    return cache[key]


def _any_line_in_head(repo: str, head: str, lines: Sequence[str]) -> bool:
    """Une des lignes apparaît-elle **quelque part** dans l'arbre de la tête ?

    Utilisé seulement quand le chemin d'origine ne porte plus la ligne : un
    fichier peut avoir été déplacé ou son contenu regroupé ailleurs (#2924
    B8). `git grep` lit l'arbre entier en un appel ; une lecture impossible
    est un doute, jamais une preuve de réécriture."""
    patterns = "\n".join(str(line).strip() for line in lines or ()
                         if str(line).strip())
    if not patterns:
        return False
    try:
        proc = plan_git._git(repo, "grep", "-F", "-q", "-f", "-", head,
                             stdin=(patterns + "\n").encode("utf-8"))
    except Exception:  # noqa: BLE001 - lecture impossible : doute
        return True
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return True


def _lines_in_tree(repo: str, rev: str, lines: Sequence[str]) -> set:
    """Les lignes (dépouillées) de `lines` présentes **telles quelles** dans
    l'arbre de `rev`, à l'indentation près.

    `git grep -F` trouve les lignes qui *contiennent* un motif ; seules celles
    qui lui sont égales une fois dépouillées comptent, pour qu'une ligne propre
    au correcteur ne soit jamais écartée parce qu'une ligne plus longue de la
    base la contient. Une lecture impossible rend l'ensemble vide : rien n'est
    écarté, le doute conserve l'auteur (#2977)."""
    wanted = {str(line).strip() for line in lines or () if str(line).strip()}
    if not repo or not rev or not wanted:
        return set()
    try:
        proc = plan_git._git(repo, "grep", "-h", "-I", "-F", "-f", "-", rev,
                             stdin=("\n".join(sorted(wanted)) + "\n").encode("utf-8"))
    except Exception:  # noqa: BLE001 - lecture impossible : rien d'écarté
        return set()
    if proc.returncode != 0:
        return set()
    found = {line.strip() for line in
             proc.stdout.decode("utf-8", "replace").splitlines()}
    return wanted & found


def _inherited_lines(repo: str, head: str, sha: str, base: str,
                     lines: Sequence[str], cache: dict) -> set:
    """Les lignes ajoutées par `sha` qui existaient déjà avant lui (#2977).

    Une ligne déjà présente dans l'arbre du parent du correctif (le gel qu'il
    corrigeait, base comprise) ou dans la base de fusion de la tête et de la
    cible n'est pas propre au correcteur : sa présence dans la tête ne prouve
    pas que le correctif subsiste (cas #2968 : `of_lot.assert_not_called()`,
    déjà dans `tests/test_1104` sur main)."""
    inherited = set(_lines_in_tree(repo, str(sha) + "^", lines))
    if base:
        key = ("merge-base", head, base)
        if key not in cache:
            cache[key] = plan_git._out(repo, "merge-base", head, base) or ""
        if cache[key]:
            inherited |= _lines_in_tree(repo, cache[key], lines)
    return inherited


def _commit_survives(repo: str, head: str, sha: str, cache: dict,
                     base: str = "", *, absent_path_keeps: bool = True,
                     moved_search: bool = True) -> bool:
    """Une partie significative du commit subsiste-t-elle dans la tête ?

    C'est la preuve de contenu de #2924, volontairement **prudente** : un
    commit n'est tenu pour réécrit que si **aucune** de ses lignes
    significatives ne subsiste. Une ligne ou un commentaire modifié, une
    réorganisation partielle ne suffisent donc jamais à retirer son auteur
    (canon §2 : le doute conserve l'auteur) ; une réécriture complète, elle,
    ne le reprend pas. Un diff illisible ou binaire conserve l'auteur.

    #2924 B8 : un chemin absent de la tête n'est pas une preuve de
    suppression. Un fichier peut avoir été déplacé (ou son contenu regroupé
    ailleurs) sans qu'une ligne soit réécrite ; l'absence du seul ancien
    chemin conserve donc l'auteur. Seul un chemin encore présent dont aucune
    ligne significative ne subsiste prouve la réécriture.

    #2977 : une ligne ajoutée qui existait déjà avant le correctif (parent du
    commit, ou base de fusion de la tête et de `base`) n'est pas propre au
    correcteur et ne prouve rien ; seules ses lignes propres sont cherchées,
    avec la même prudence (chemin d'origine, puis tout l'arbre, B8). Une ligne
    que le commit retire aussi ailleurs (un déplacement) reste une preuve,
    comme avant. Sans ligne propre, la lecture passe aux lignes retirées,
    puis aux lignes copiées, cherchées à leur seul chemin (un chemin absent
    conserve l'auteur) : une copie retirée ne garde pas son correcteur (B1).
    Un diff sans ligne significative conserve l'auteur.

    `absent_path_keeps=False` (un **inconnu** dont on décide s'il bloque,
    #3116) retire le doute B8 : un chemin absent dont aucune ligne ne se
    retrouve dans l'arbre de la tête ne prouve plus la présence du contenu.
    `moved_search=False` écarte en plus la recherche de lignes génériques dans
    tout l'arbre : pour un inconnu, une ligne courte comme `case "$http" in`
    trouvée dans un autre fichier n'est pas une preuve de contenu. Les auteurs
    gardent les défauts (`True`) : le doute conserve l'auteur."""
    parsed = _commit_lines(repo, sha)
    if parsed is None:
        return True
    adds, removes, binary = parsed
    if binary:
        return True
    added = [(path, row) for path, rows in adds.items() for row in rows
             if _meaningful_line(row)]
    copied: list = []
    if added:
        moved = {row.strip() for rows in removes.values() for row in rows}
        inherited = _inherited_lines(
            repo, head, sha, base,
            [row for _path, row in added if row.strip() not in moved], cache)
        copied = [(path, row) for path, row in added
                  if row.strip() in inherited]
        added = [(path, row) for path, row in added
                 if row.strip() not in inherited]
    if added:
        missing_path = False
        for path, row in added:
            lines = _head_lines(repo, head, path, cache)
            if lines is None:
                missing_path = True
            elif row.strip() in lines:
                return True
        # Le chemin d'origine ne porte plus la ligne : elle a pu être déplacée
        # avec son fichier ou regroupée ailleurs. La chercher dans tout l'arbre
        # évite d'effacer le correcteur sur un simple déplacement (#2924 B8).
        if moved_search and _any_line_in_head(
                repo, head, [row for _path, row in added]):
            return True
        return missing_path and absent_path_keeps
    removed = [(path, row) for path, rows in removes.items() for row in rows
               if _meaningful_line(row)]
    if removed and any(
            row.strip() not in (_head_lines(repo, head, path, cache) or set())
            for path, row in removed):
        return True
    if copied:
        # #2977 B1 : des additions significatives, toutes héritées. La copie
        # ne subsiste qu'à son propre chemin (pas de recherche dans l'arbre :
        # l'original y est toujours) ; un chemin absent reste un doute. B2 : au
        # même chemin, l'original peut déjà y figurer ; seule une occurrence de
        # plus que dans le parent prouve la copie (parent sans ce chemin : 0).
        for path, row in copied:
            present = _path_line_counts(repo, head, path, cache)
            if present is None:
                return absent_path_keeps
            before = _path_line_counts(repo, str(sha) + "^", path, cache) or {}
            if present.get(row.strip(), 0) > before.get(row.strip(), 0):
                return True
        return False
    return not removed


def _freeze_content_preserved(repo: str, freeze_sha: str, head_sha: str,
                              base: str = "") -> bool:
    """Le contenu du gel est-il encore celui de la tête (même arbre) ?

    `True` quand l'arbre est identique, ou quand la tête est exactement la
    fusion du gel avec la base (une consolidation sur une cible avancée). Un
    arbre illisible ou une fusion conflictuelle ne prouvent pas une réécriture :
    le doute rend `True` (les auteurs restent)."""
    head_tree = _tree_of(repo, head_sha)
    freeze_tree = _tree_of(repo, freeze_sha)
    if head_tree is None or freeze_tree is None:
        return True
    if head_tree == freeze_tree:
        return True
    if base:
        merged = _merged_tree(repo, freeze_sha, base)
        if merged is None or merged == head_tree:
            return True
    return False


def _freeze_reference(db, item: Mapping, freeze: Mapping, families) -> dict:
    """L'auteur de référence d'un gel antérieur `{name, family}`.

    Même autorité que `_author_at_freeze` : l'acteur du gel s'il a tenu le lot,
    sinon l'assigné. La famille vient des tours tracés à l'instant du gel."""
    freeze = dict(freeze or {})
    item = dict(item or {})
    actor = str(freeze.get("actor") or "").strip()
    assignee = str(item.get("assignee") or "").strip()
    name = actor or assignee
    try:
        holders = review_lot._holders(db, int(item.get("id") or 0), item)
    except Exception:  # noqa: BLE001 - journal illisible : l'assigné seul
        holders = {assignee}
    if name not in holders and assignee:
        name = assignee
    family = ""
    at_ts = freeze.get("at_ts")
    if name and at_ts is not None:
        try:
            family = _family(review_lot._turns_harness(db, name, float(at_ts)),
                             families)
        except Exception:  # noqa: BLE001 - trace illisible : famille inconnue
            family = ""
    return {"name": name, "family": family}


def _prior_reading(db, item: Mapping, freeze: Mapping, commits: Sequence[str],
                   repo: str, families, *, provider=None,
                   strict: bool = False, target: str = "", head: str = "",
                   base: str = "", cache: Optional[dict] = None
                   ) -> tuple[dict, dict, dict, list]:
    """`(auteurs, commits_écrits, corrections, inconnus)` d'un gel antérieur.

    Même attribution que `reading` — reçu exact, puis committer d'adresse
    d'agent et journal des tours (#2721) — mais sans la reprise des gels
    encore plus anciens. `commits_écrits` associe à chaque acteur les commits
    qu'il a écrits : c'est la preuve de contenu de #2924. `corrections` porte
    les commits des relecteurs désignés ; leur qualification de « correctif de
    premier tour » se fait dans `_prior_correctors` (#2888).

    #3116 : `target` est la cible au moment de la lecture. Ses commits ne
    comptent ni comme auteurs ni comme tiers. Hors cible, un inconnu qui n'est
    plus ancêtre de `head` et dont le contenu ne survit pas ne bloque plus
    (`_survives_rewrite`) ; `cache` partage la preuve de contenu."""
    reference = _freeze_reference(db, item, freeze, families)
    author_name = str(reference.get("name") or "").strip()
    author_family = str(reference.get("family") or "")
    reviewers = designated_reviewers(db, item, freeze, author_name)
    by_name: dict[str, set] = {}
    written: dict[str, list] = {}
    review_commits: dict[str, list] = {}
    unknown: list[str] = []
    cache = {} if cache is None else cache
    commits = _not_in_target(repo, commits, target)

    def record_unknown(value: str) -> None:
        """Un inconnu ne bloque que s'il compte encore pour la tête (#3116)."""
        if _survives_rewrite(repo, head, value, cache, base, target):
            unknown.append(str(value)[:12])

    for raw in commits or ():
        sha = str(raw or "").strip()
        if not sha:
            continue
        try:
            receipt = _commit_receipt(provider, db, sha, strict=strict)
        except review_provenance.ProvenanceError:
            if strict:
                raise
            record_unknown(sha)
            continue
        if receipt is None:
            actor, stamp = commit_actor(repo, sha)
            if not actor:
                record_unknown(sha)
                continue
            if actor != author_name:
                try:
                    known = registry.get(db, actor) is not None
                except Exception:  # noqa: BLE001 - registre illisible : inconnu
                    known = False
                if not known:
                    record_unknown(sha)
                    continue
            if actor == author_name:
                proven = actor_family(db, actor, stamp, families) or author_family
                by_name.setdefault(actor, set()).add(proven)
                written.setdefault(actor, []).append(sha)
                continue
            if actor in reviewers:
                proven = actor_family(db, actor, stamp, families)
                review_commits.setdefault(actor, []).append((sha, proven))
                written.setdefault(actor, []).append(sha)
                continue
            record_unknown(sha)
            continue
        actor = str(receipt.get("actor") or "").strip()
        if not actor:
            record_unknown(sha)
            continue
        proven = _family(receipt.get("harness"), families)
        by_name.setdefault(actor, set()).add(proven)
        written.setdefault(actor, []).append(sha)
        if actor != author_name and actor in reviewers:
            review_commits.setdefault(actor, []).append((sha, proven))
    # #2924 B4 : la lecture courante promeut tout commit de relecteur en auteur
    # (#2888). La lecture d'un gel antérieur doit faire de même, même quand le
    # commit n'a pas de reçu exact et que la famille vient du journal des tours
    # (#2721) : sinon un squash à contenu identique perdrait cette famille.
    for actor, entries in review_commits.items():
        by_name.setdefault(actor, set())
        for _sha, proven in entries:
            if proven:
                by_name[actor].add(proven)
    return by_name, written, review_commits, unknown


def _prior_correctors(repo: str, review_commits: Mapping) -> dict:
    """Les correctifs de premier tour d'un gel antérieur, par acteur.

    Même seuil que `reading` (#2888) : le seuil ne décide que de la trace
    « correcteur », jamais de l'auteur. Un diff illisible ou de conception
    n'est pas un petit correctif."""
    out: dict = {}
    for actor, entries in review_commits.items():
        total, design = 0, False
        for sha, _proven in entries:
            measured = change(repo, sha)
            if measured is None or measured[1]:
                design = True
                break
            total += measured[0]
        if design or total > REVIEWER_FIX_LINES:
            continue
        proven = {value for _sha, value in entries if value}
        family = next(iter(proven)) if len(proven) == 1 else ""
        # #2924 B6 : l'ensemble des familles prouvées est transporté, pas
        # seulement sa réduction à une famille unique (vide si ambiguë).
        out[actor] = {"name": actor, "family": family,
                      "families": sorted(proven)}
    return out


def _superseded_freeze_ids(freezes: Sequence[Mapping]) -> set[int]:
    """Les gels qu'un gel postérieur déclare remplacés (#2989).

    La marque est écrite par `ameesh work milestone frozen --supersedes <id>`
    (jamais recopiée d'une note d'appelant) : elle vaut pour un gel dont le SHA
    n'existe nulle part, le cas d'un gel fantôme écrit avant la validation de
    la CLI. Elle ne dispense jamais de lire un gel encore présent dans le
    dépôt : le refus fermé de #2924 B5 reste la règle quand le contenu existe."""
    from .freeze_gate import FREEZE_SUPERSEDES_RE
    out: set[int] = set()
    for row in freezes or ():
        note = str(row.get("note") or "")
        for match in FREEZE_SUPERSEDES_RE.finditer(note):
            try:
                out.add(int(match.group(1)))
            except (TypeError, ValueError):
                continue
    return out


def _carried_from_previous(db, item: Mapping, milestone: Optional[Mapping],
                           commits: Sequence[str], repo: str, families, *,
                           base: str = "", head: str = "", provider=None,
                           strict: bool = False, target: str = "",
                           recover=None) -> tuple[list, list, list]:
    """Auteurs/correcteurs des gels antérieurs dont le contenu subsiste (#2924).

    Rend `(auteurs, inconnus, correcteurs)` à fusionner dans la lecture de la
    tête. Un lot porteur de paquet, une lecture sans base de données ou sans
    dépôt, ou un lot sans gel antérieur rendent des listes vides. Toute
    provenance illisible d'un gel antérieur qui n'est pas déjà couverte par la
    plage courante ajoute un inconnu : l'appelant strict refuse, la désignation
    attend (jamais de famille supposée).

    #2924 B7 : `recover(sha)` est le seul moyen autorisé de réparer un gel
    antérieur absent du dépôt de lecture (le clone scratch de `merge-pr` ne
    porte que la cible et la tête). Il est appelé **avant** de déclarer le gel
    illisible ; s'il échoue, le refus fermé de B5 reste la règle. Le lecteur
    direct (sans `recover`) garde le refus : une absence non réparée ne prouve
    jamais l'absence d'un correcteur.

    #2989 : un gel **déclaré remplacé** par un gel postérieur et dont le SHA
    est absent (même après `recover`) est ignoré : c'est un gel fantôme, son
    contenu n'a jamais existé, et le blocage de la provenance serait définitif.
    La note du gel remplaçant porte la trace de la déclaration. Un gel remplacé
    mais encore lisible est lu comme avant (B5).

    #3116 : `target` est la cible au moment de la lecture (un commit ou une
    ref). Un commit qui y est déjà entré a été relu sous son propre lot : il ne
    compte ni comme auteur ni comme tiers, même s'il apparaît dans la plage
    d'un gel antérieur calculée avant un rebase. Un gel entièrement contenu
    dans la cible n'est pas relu. Sans `target` lisible, `base` (toujours un
    ancêtre de la cible) sert de borne sûre."""
    item = dict(item or {})
    if db is None or not repo or packet_composer(item):
        return [], [], []
    lot = int(item.get("id") or 0)
    if not lot:
        return [], [], []
    try:
        rows = storage.of(db).work.milestones(lot, None) or []
    except Exception as exc:  # noqa: BLE001 - jalons illisibles : jamais ouverts
        if strict:
            raise review_provenance.ProvenanceError(
                "the freeze milestones of lot #%d cannot be read (%s)"
                % (lot, exc)) from exc
        return [], ["milestones:%d" % lot], []
    freezes = sorted((row for row in rows if row.get("kind") == "frozen"),
                     key=lambda value: (value.get("at_ts") or 0,
                                        int(value.get("id") or 0)))
    if not freezes:
        return [], [], []
    superseded = _superseded_freeze_ids(freezes)
    head_sha = str(head or (milestone or {}).get("sha") or "").strip()
    current = None
    head_id = int((milestone or {}).get("id") or 0)
    if head_id:
        current = next((row for row in freezes
                        if int(row.get("id") or 0) == head_id), None)
    if current is None and head_sha:
        current = next((row for row in reversed(freezes)
                        if plan_git.same_commit(row.get("sha"), head_sha)), None)
    if not head_sha:
        # La désignation passe le gel ; la porte le jalon. Sans l'un ni l'autre
        # (lecture directe), le gel le plus récent présent dans la plage est la
        # tête — sinon le premier commit de la plage en est le représentant.
        for row in reversed(freezes):
            sha = str(row.get("sha") or "").strip()
            if sha and any(plan_git.same_commit(sha, value)
                           for value in commits or ()):
                head_sha, current = sha, row
                break
        if not head_sha:
            head_sha = str((commits or [""])[0] or "").strip()
            current = next((row for row in reversed(freezes)
                            if plan_git.same_commit(row.get("sha"), head_sha)),
                           None)
    if not head_sha:
        return [], [], []
    target_sha = target_commit(repo, target) or target_commit(repo, base)
    if current is not None:
        prior = [row for row in freezes
                 if int(row.get("id") or 0) < int(current.get("id") or 0)]
    else:
        in_range = {plan_git.full_commit_id(value) for value in commits or ()}
        prior = [row for row in freezes
                 if str(row.get("sha") or "").strip()
                 and plan_git.full_commit_id(row.get("sha")) not in in_range]
    if not prior:
        return [], [], []
    current_set = {plan_git.full_commit_id(value) for value in commits or ()}
    carried: dict[str, set] = {}
    correctors: dict[str, dict] = {}
    unknown: list[str] = []
    processed: list[str] = []
    survival: dict[str, bool] = {}
    file_cache: dict = {}

    def survives(written: Mapping, stored: bool, actor: str, *,
                 every: bool = False) -> bool:
        """Le contenu écrit par `actor` dans ce gel subsiste-t-il dans la tête ?"""
        shas = written.get(actor) or []
        if not shas:
            return False
        if stored:
            return True
        values = []
        for commit in shas:
            if commit not in survival:
                survival[commit] = _commit_survives(
                    repo, head_sha, commit, file_cache, base)
            values.append(survival[commit])
        return all(values) if every else any(values)

    def present(commit: str) -> bool:
        """Le commit est-il lisible **localement** (jamais un fetch paresseux) ?"""
        try:
            return bool(plan_git.local_commit(repo, str(commit or "").strip()))
        except Exception:  # noqa: BLE001 - dépôt illisible : absence
            return False

    def recover_one(commit: str) -> bool:
        """Tente de rapatrier un gel absent ; True seulement s'il est lisible."""
        if recover is None:
            return False
        try:
            if present(commit):
                return True
            return bool(recover(commit)) and present(commit)
        except Exception:  # noqa: BLE001 - récupération en échec : absence
            return False

    for row in sorted(prior, key=lambda value: (value.get("at_ts") or 0,
                                                int(value.get("id") or 0)),
                      reverse=True):
        row_id = int(row.get("id") or 0)
        if row_id in superseded:
            # #2989 : un gel déclaré remplacé dont le SHA n'existe nulle part
            # (même après récupération) est un gel fantôme : son contenu n'a
            # jamais existé, et le refus fermé bloquerait la provenance pour
            # toujours. La note du gel remplaçant porte la trace (marque
            # machine + événement de gel). Un gel encore lisible est lu.
            prior_sha = str(row.get("sha") or "").strip()
            if not prior_sha or (not present(prior_sha)
                                 and not recover_one(prior_sha)):
                continue
        sha = str(row.get("sha") or "").strip()
        if not sha:
            if strict:
                raise review_provenance.ProvenanceError(
                    "prior freeze #%s of lot #%d names no commit: its authors "
                    "cannot be carried" % (row.get("id"), lot))
            unknown.append("freeze:%d" % int(row.get("id") or 0))
            continue
        # un gel déjà attribué par la plage courante n'a pas besoin d'objets
        if plan_git.full_commit_id(sha) in current_set:
            continue
        if not present(sha) and not recover_one(sha):
            # #2924 B7 : sans les objets, impossible de prouver qu'aucun
            # correcteur n'est en cause ; B5 refuse plutôt que de deviner.
            if strict:
                raise review_provenance.ProvenanceError(
                    "prior freeze %s of lot #%d is absent from the repository "
                    "and cannot be recovered: its authors cannot be carried"
                    % (sha[:12], lot))
            unknown.append(sha[:12])
            continue
        if base and not present(base):
            recover_one(base)
        if head_sha and not present(head_sha):
            recover_one(head_sha)
        # le contenu d'un gel déjà entré dans la cible n'est pas relu (#3116) :
        # ses commits ont été relus sous leur propre lot, jamais sous celui-ci.
        if target_sha and _is_ancestor(repo, sha, target_sha) is True:
            continue
        if base and _is_ancestor(repo, sha, base) is True:
            continue
        # un gel plus récent déjà traité couvre celui-ci (même histoire)
        if any(_is_ancestor(repo, sha, kept) is True for kept in processed):
            continue
        start = plan_git._out(repo, "merge-base", sha, base or head_sha)
        if not start:
            if strict:
                raise review_provenance.ProvenanceError(
                    "prior freeze %s of lot #%d is not readable against the base: "
                    "its authors cannot be carried" % (sha[:12], lot))
            unknown.append(sha[:12])
            continue
        earlier = _commits_between(repo, start, sha)
        if earlier is not None:
            earlier = _not_in_target(repo, earlier, target_sha)
        if earlier is None:
            if strict:
                raise review_provenance.ProvenanceError(
                    "the commit range of prior freeze %s of lot #%d is unreadable"
                    % (sha[:12], lot))
            unknown.append(sha[:12])
            continue
        processed.append(sha)
        if not earlier or all(plan_git.full_commit_id(value) in current_set
                              for value in earlier):
            continue                     # déjà attribué par la plage courante
        try:
            by_name, written, review_commits, unreadable = _prior_reading(
                db, item, row, earlier, repo, families,
                provider=provider, strict=strict, target=target_sha,
                head=head_sha, base=base, cache=file_cache)
        except Exception as exc:  # noqa: BLE001 - un gel antérieur illisible
            if strict:
                raise review_provenance.ProvenanceError(
                    "the authors of prior freeze %s of lot #%d cannot be read (%s)"
                    % (sha[:12], lot, exc)) from exc
            unknown.append(sha[:12])
            continue
        unknown.extend(unreadable)
        ancestor = _is_ancestor(repo, sha, head_sha)
        stored = (True if ancestor is True
                  else _freeze_content_preserved(repo, sha, head_sha, base))
        for actor, proven in by_name.items():
            if not survives(written, stored, actor):
                continue
            carried.setdefault(actor, set()).update(
                value for value in proven if value)
        for actor in review_commits:
            if not survives(written, stored, actor, every=True):
                continue
            if actor in correctors:
                continue
            correctors.update(_prior_correctors(repo, {actor: review_commits[actor]}))
    authors = []
    for actor in sorted(carried):
        proven = {value for value in carried[actor] if value}
        authors.append({"name": actor,
                        "family": next(iter(proven)) if len(proven) == 1 else "",
                        # #2924 B6 : un acteur peut avoir écrit sous plusieurs
                        # familles dans un gel antérieur ; la lecture courante
                        # doit recevoir tout l'ensemble, pas la seule réduction
                        # ambiguë à "".
                        "families": sorted(proven)})
    return authors, unknown, [correctors[name] for name in sorted(correctors)]


def reading(db, item: Mapping, milestone: Optional[Mapping],
            commits: Sequence[str], repo: str = "", families=(),
            *, reference: Optional[Mapping] = None,
            reference_families: Optional[Mapping] = None, provider=None,
            strict: bool = False, trace: bool = False,
            families_out: Optional[dict] = None,
            base: str = "", head: str = "", target: str = "",
            recover=None) -> tuple[list, list, list]:
    """`(auteurs, inconnus, correcteurs)` de la plage gelée, même lecture pour tous.

    `reference` est l'auteur de référence déjà lu (`reference_author`) : la
    désignation et le train la partagent. `reference_families` porte les
    familles prouvées de cette référence (le `families_out` de
    `reference_author`, #2961) : deux familles différentes sont une ambiguïté
    et sont ajoutées aux preuves de l'auteur. `provider` est le fournisseur
    exact de reçus (#1465) ; `strict` fait d'un reçu corrompu une erreur (le
    train), au lieu d'une absence (la désignation attend). `trace=True`
    (désignation) ajoute aux auteurs du repli `proof: turn-journal` et
    `fallback_commits` ; le train et la porte gardent la forme exacte
    `{name, family}`. `families_out` reçoit, par acteur en minuscules,
    l'ensemble des familles **prouvées** : une famille vide peut être une
    absence de preuve ou une ambiguïté, et l'appelant qui doit les distinguer
    ne peut pas la deviner.

    Un commit sans reçu est attribué au committer d'adresse d'agent : un commit
    du titulaire de référence reste le sien ; un relecteur désigné reste
    l'auteur de sa correction, avec sa famille de session ; un tiers non
    relecteur reste inconnu. Le seuil du §2 ne décide que de la trace « correctif
    de premier tour », jamais de l'auteur (#2888).

    Les **correcteurs** (#2739) sont les relecteurs désignés dont le correctif
    de premier tour reste sous le seuil. Ce sont des **auteurs de la tête**
    (#2888) : leur famille est exclue comme celle de tout auteur, et l'agent
    lui-même ne relit pas son propre correctif. `correctors` garde leur trace
    (provenance réelle) pour l'exclusion personnelle ; ce n'est plus une
    éligibilité de famille. Un correctif au-delà du seuil ou de conception est
    aussi un auteur, mais n'est pas un « correcteur » de premier tour.

    #2721 : la famille d'un commit sans reçu vient du journal des tours **à
    l'instant du commit** (le harnais qui a réellement tourné, harnais emprunté
    compris), pas du harnais déclaré ; à défaut, le titulaire de référence
    garde sa famille de gel. Un committer distinct de la référence doit être un
    agent du registre. Les commits admis par ce repli portent la trace
    `proof: turn-journal` et leurs SHA dans `fallback_commits` quand
    `trace=True` (désignation).

    #2808 : le titulaire n'est **pas semé** dans les auteurs par sa seule
    qualité d'assigné au gel. Il n'entre dans `by_name` que par un commit de la
    plage : auteur Git (`%ae`, un amend conserve l'auteur d'origine), reçu
    exact, ou repli #2721 pour son propre commit de committer. Un titulaire
    réassigné sans commit ne ferme donc plus sa famille (#2805) ; une
    provenance illisible reste `unknown` et la désignation attend, la porte
    refuse fermé.

    #2803 : sur un lot porteur de paquet (`integration/**`), la référence est
    le composeur mécanique et n'est pas un auteur (`packet_composer`). Le
    commit de manifeste (`reviews/integration-*.md`, écrit par le composeur)
    est écarté de la même lecture : les auteurs sont ceux des lots du
    manifeste, ni le composeur ni sa famille.

    #2924 : les auteurs des **gels antérieurs du même lot** dont le contenu
    subsiste restent auteurs de cette tête. Une réécriture d'historique à
    contenu identique (consolidation, squash sur une cible avancée) ne doit pas
    effacer la provenance des correcteurs : leurs familles restent exclues
    comme celles de tout auteur. `base` (la base de fusion de la tête) et
    `head` (la tête lue) permettent la preuve de contenu ; sans eux, le gel le
    plus récent présent dans `commits` tient lieu de tête. Un contenu
    réellement réécrit n'est pas repris ; le doute conserve l'auteur.
    `recover(sha)` (#2924 B7) permet à la porte de rapatrier un gel antérieur
    absent de son clone scratch avant de refuser ; sans lui, le lecteur garde
    le refus fermé de B5. Un acteur prouvé sous plusieurs familles dans un gel
    antérieur garde tout l'ensemble (`families_out`), jamais la seule famille
    du reçu courant.

    #3116 : `target` est la cible au moment de la lecture (commit ou ref). Un
    commit qui y est déjà entré ne compte ni comme auteur ni comme tiers du
    lot, qu'il vienne de la plage courante ou du report d'un gel antérieur :
    il a été relu sous son propre lot. Sans `target`, `base` — toujours un
    ancêtre de la cible — sert de borne sûre ; sans l'un ni l'autre, la
    lecture garde son comportement d'avant (aucun commit écarté).
    """
    item = dict(item or {})
    packet = bool(packet_composer(item))
    target_sha = target_commit(repo, target) or target_commit(repo, base)
    commits = _not_in_target(repo, commits, target_sha)
    proven_reference: dict = dict(reference_families or {})
    if reference is None:
        reference = reference_author(db, item, milestone, families,
                                     provider=provider, strict=strict,
                                     families_out=proven_reference)
    author_name = str((reference or {}).get("name") or "").strip()
    author_family = str((reference or {}).get("family") or "")
    reviewers = designated_reviewers(db, item, milestone, author_name)
    by_name: dict[str, set] = {}
    unknown: list[str] = []
    review_commits: dict[str, list[tuple[str, str]]] = {}
    proofs: dict[str, set[str]] = {}
    for raw in commits or ():
        sha = str(raw or "").strip()
        if not sha:
            continue
        if packet and _manifest_commit(repo, sha):
            continue                      # composition mécanique du composeur (#2803)
        try:
            receipt = _commit_receipt(provider, db, sha, strict=strict)
        except review_provenance.ProvenanceError:
            if strict:
                raise
            unknown.append(sha[:12])
            continue
        if receipt is None:
            actor, stamp = commit_actor(repo, sha)
            if not actor:
                unknown.append(sha[:12])
                continue
            if actor != author_name:
                try:
                    known = registry.get(db, actor) is not None
                except Exception:  # noqa: BLE001 - registre illisible : inconnu
                    known = False
                if not known:
                    # un committer distinct de la référence doit être un agent
                    # du registre : un auteur forgé n'achète pas une famille
                    unknown.append(sha[:12])
                    continue
            if actor == author_name:
                # le journal du tour du commit d'abord : c'est le harnais qui
                # a réellement tourné ; la famille de gel prend le relais
                proven = actor_family(db, actor, stamp, families) or author_family
                by_name.setdefault(actor, set()).add(proven)
                proofs.setdefault(actor, set()).add(sha[:12])
                continue
            if actor in reviewers:
                proven = actor_family(db, actor, stamp, families)
                review_commits.setdefault(actor, []).append((sha, proven))
                proofs.setdefault(actor, set()).add(sha[:12])
                continue
            # Un tiers sans reçu n'est pas attribuable : aucun camp ne le
            # suppose, même si un relecteur porte le même nom.
            unknown.append(sha[:12])
            continue
        actor = str(receipt.get("actor") or "").strip()
        if not actor:
            unknown.append(sha[:12])
            continue
        proven = _family(receipt.get("harness"), families)
        if actor == author_name:
            by_name.setdefault(actor, set()).add(proven)
        elif actor in reviewers:
            # sa correction est un contenu de la tête (#2888) ; le seuil ne
            # décide que de la trace « correcteur de premier tour »
            review_commits.setdefault(actor, []).append((sha, proven))
        else:
            by_name.setdefault(actor, set()).add(proven)
    # #2808 : le titulaire n'est un auteur que par un commit de la plage.
    # Quand il en a écrit un (reçu/committer, ou auteur Git conservé par un
    # amend), sa famille de gel reste une preuve historique, en plus de celle
    # du commit : deux familles prouvées sont ambiguës et font attendre
    # (#2755). Un titulaire sans commit n'ajoute ni nom ni famille.
    # #2961 B2 : la référence peut avoir deux familles prouvées (journal du gel
    # + reçu du jalon) ; la lecture les rend toutes pour que la porte refuse,
    # au lieu d'en choisir une.
    authored = ""
    if author_name:
        authored = next((name for name in by_name
                         if name.lower() == author_name.lower()), "")
        if not authored:
            written = commits_written_by(repo, commits, author_name)
            if written is None:
                unknown.extend(str(sha)[:12] for sha in commits if str(sha)[:12] not in unknown)
            elif written:
                authored = author_name
                by_name.setdefault(authored, set())
        if authored:
            for family in proven_reference.get(author_name.lower()) or ():
                if family:
                    by_name[authored].add(family)
            if author_family:
                by_name[authored].add(author_family)
    # #2888 : toute correction d'un relecteur désigné fait de lui un auteur de
    # la tête, quelle que soit sa taille. Le seuil du §2 ne décide que de la
    # recevabilité process (une correction trop grosse revient à l'auteur),
    # jamais de l'indépendance : sa famille n'est plus éligible.
    small_correctors: set[str] = set()
    for actor, entries in sorted(review_commits.items()):
        total, design = 0, False
        for sha, _proven in entries:
            measured = change(repo, sha)
            if measured is None or measured[1]:
                design = True
                break
            total += measured[0]
        by_name.setdefault(actor, set())
        for _sha, proven in entries:
            if proven:
                by_name[actor].add(proven)
        if not design and total <= REVIEWER_FIX_LINES:
            small_correctors.add(actor)
    # #2924 : les auteurs des gels antérieurs dont le contenu subsiste restent
    # auteurs de cette tête. Une réécriture à contenu identique ne les efface
    # pas ; un contenu réellement réécrit n'est pas repris (le doute conserve).
    carried, carried_unknown, carried_correctors = _carried_from_previous(
        db, item, milestone, commits, repo, families, base=base, head=head,
        provider=provider, strict=strict, target=target_sha, recover=recover)
    unknown.extend(carried_unknown)
    for row in carried:
        values = row.get("families")
        if values is None:
            values = (row.get("family"),)
        # #2924 B6 : fusionner toutes les familles prouvées, y compris quand
        # l'acteur a aussi une preuve courante ; la réduction ambiguë à "" ne
        # doit jamais effacer l'autre famille.
        by_name.setdefault(str(row["name"]), set()).update(
            str(value) for value in values if str(value or "").strip())
    out = []
    for actor in sorted(by_name):
        proven = {value for value in by_name[actor] if value}
        if families_out is not None:
            families_out[actor.lower()] = set(proven)
        # un acteur à deux familles prouvées est ambigu : jamais deviné
        family = next(iter(proven)) if len(proven) == 1 else ""
        row = {"name": actor, "family": family}
        if trace and proofs.get(actor):
            # #2721 : la trace est celle de la désignation (`trace=True`) ; le
            # train et la porte gardent la forme exacte `{name, family}`.
            row["proof"] = "turn-journal"
            row["fallback_commits"] = sorted(proofs[actor])
        out.append(row)
    corrector_rows: dict[str, dict] = {}

    def record_corrector(name: str, family: str, families_values=()) -> None:
        """Trace un correcteur en gardant **toutes** ses familles prouvées."""
        name = str(name or "").strip()
        if not name:
            return
        key = name.lower()
        proven = {str(value) for value in families_values if str(value or "").strip()}
        if str(family or "").strip():
            proven.add(str(family))
        row = corrector_rows.get(key)
        if row is None:
            row = {"name": name, "family": "", "proven": set()}
            corrector_rows[key] = row
        row["proven"].update(proven)
        # une ambiguïté de familles ne change rien à l'exclusion par le nom,
        # mais elle ne doit pas être présentée comme une famille unique.
        row["family"] = (next(iter(row["proven"]))
                         if len(row["proven"]) == 1 else "")
        if families_out is not None:
            families_out.setdefault(key, set()).update(row["proven"])

    for actor in sorted(small_correctors):
        proven = {value for _sha, value in review_commits[actor] if value}
        record_corrector(actor, "", proven)
    # #2924 : un correcteur d'un gel antérieur dont le correctif subsiste garde
    # sa trace ; il est déjà auteur de la tête ci-dessus (l'exclusion de famille
    # ne dépend pas de cette liste).
    for row in carried_correctors:
        record_corrector(str(row.get("name") or ""),
                         str(row.get("family") or ""),
                         row.get("families") or ())
    correctors = [{"name": row["name"], "family": row["family"]}
                  for row in corrector_rows.values()]
    return out, unknown, correctors


def authors(db, item: Mapping, milestone: Optional[Mapping],
            commits: Sequence[str], repo: str = "", families=(),
            *, reference: Optional[Mapping] = None,
            reference_families: Optional[Mapping] = None, provider=None,
            strict: bool = False, trace: bool = False,
            base: str = "", head: str = "", target: str = "",
            recover=None) -> tuple[list, list]:
    """`(auteurs, inconnus)` — la lecture partagée, sans les correcteurs."""
    authors_, unknown, _correctors = reading(
        db, item, milestone, commits, repo, families, reference=reference,
        reference_families=reference_families, provider=provider,
        strict=strict, trace=trace, base=base, head=head, target=target,
        recover=recover)
    return authors_, unknown


def verdict_families(db, marks, families, *, provider=None,
                     strict: bool = False,
                     require_receipt: bool = False) -> dict[str, str]:
    """Les familles historiques des relecteurs d'une tête gelée.

    Pour chaque acteur, seul son **dernier** verdict de la liste compte (la
    porte lit ce même dernier avis) : une preuve d'un avis supplanté ne peut
    ni refuser ni changer sa famille. Le reçu exact du jalon est la preuve ;
    sans lui, le journal des tours à l'instant du verdict. Une preuve
    contradictoire est une erreur si `strict`, une absence sinon. Le résultat
    est indexé par acteur en minuscules, la casse de la porte.

    `require_receipt` (#2928) : en classe sensible, un verdict sans reçu exact
    ne compte pas. Les avis sans preuve sont alors écartés **avant** le choix
    du dernier par acteur, comme à la porte : un faux `ok` sans reçu ne peut
    ni fournir une famille ni masquer un vrai dernier avis.
    """
    marks = [mark for mark in (marks or ()) if mark.get("kind") == "verdict"]
    proven_ids: set[int] | None = None
    if require_receipt:
        proven_ids = set()
        for mark in marks:
            try:
                marker = int(mark["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if str(mark.get("actor") or "").strip().startswith("human:"):
                # la règle vise un verdict d'agent : un humain n'a pas de tour.
                proven_ids.add(marker)
                continue
            # `strict` garde son sens : une preuve contradictoire est une
            # erreur (le train), pas une simple absence.
            if _milestone_receipt(provider, db, marker, strict=strict) is not None:
                proven_ids.add(marker)
    latest: dict[str, dict] = {}
    for mark in marks:
        if proven_ids is not None:
            try:
                marker = int(mark["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if marker not in proven_ids:
                continue
        actor = str(mark.get("actor") or "").strip()
        key = actor.lower()
        if not key:
            continue
        order = (float(mark.get("at_ts") or 0.0), int(mark.get("id") or 0))
        if key not in latest or order > latest[key][0]:
            latest[key] = (order, mark)
    out: dict[str, str] = {}
    for key, (_order, mark) in latest.items():
        try:
            receipt = _milestone_receipt(provider, db, int(mark["id"]), strict=strict)
        except (review_provenance.ProvenanceError, KeyError, TypeError, ValueError):
            if strict:
                raise
            receipt = None
        if receipt is not None:
            if strict:
                if str(receipt.get("actor") or "").strip().lower() != key:
                    raise review_provenance.ProvenanceError(
                        "historical verdict receipt names different facts: milestone:%s"
                        % mark["id"])
                expected = {name: mark.get(name) for name in
                            ("id", "actor", "at_ts", "sha", "verdict")}
                actual = {name: (receipt.get("milestone") or {}).get(name)
                          for name in expected}
                if actual != expected:
                    raise review_provenance.ProvenanceError(
                        "historical verdict receipt names different facts: milestone:%s"
                        % mark["id"])
            out[key] = _family(receipt.get("harness"), families)
            continue
        try:
            stamp = float(mark.get("at_ts") or 0.0)
        except (TypeError, ValueError):
            stamp = 0.0
        out[key] = actor_family(db, str(mark.get("actor") or ""), stamp, families)
    return out
