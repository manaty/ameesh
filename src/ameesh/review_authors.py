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
  ne la reprend pas. C'est la survie du contenu qui décide (#3220) : un fichier
  déplacé, un contenu identique réécrit par un autre, ou le fichier corrigé
  repris dans un chemin existant **puis enrichi** garde l'auteur ; un fichier
  supprimé, un réordre réécrit ou des lignes toutes disparues ne le gardent
  pas ; une ligne retirée ne prouve que si la base de fusion l'a portée au
  même chemin (#2994/#2614) ; une lecture impossible, elle, conserve toujours
  l'auteur. Un gel antérieur absent du clone de lecture est demandé à la forge
  (`origin`) par SHA exact, quel que soit l'appelant (#3147) : le résultat de
  la porte ne dépend pas du clone ;
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


def _registered(db, name: str) -> bool:
    if db is None or not name:
        return False
    try:
        return registry.get(db, name) is not None
    except Exception:  # noqa: BLE001 - registre illisible : inconnu
        return False


def _agent_of_email(db, email: str, stamp: float) -> str:
    """L'agent d'une adresse de commit, ou "" (#3111).

    `<nom>@ameesh.invalid` comme avant ; l'ancien `<nom>@ameesh.local` d'un
    commit d'avant la convention (`identity.LEGACY_GIT_EMAIL_UNTIL`) seulement
    vers un agent du registre, même quand il est la référence. La famille se
    prouve ensuite comme pour tout commit sans reçu : journal des tours."""
    from . import identity

    name = identity.agent_of_git_email(email, stamp, legacy=False)
    if name:
        return name
    name = identity.agent_of_git_email(email, stamp)
    return name if name and _registered(db, name) else ""


def commit_actor(repo: str, sha: str, db=None) -> tuple[str, float]:
    """`(acteur, instant)` du committer d'adresse d'agent, sinon ("", 0).

    Le committer est l'agent qui a créé l'objet (un rebase le réécrit) ; le nom
    Git seul ne prouve rien, l'adresse `<nom>@ameesh.invalid` est celle du
    runner. L'ancienne adresse `<nom>@ameesh.local` n'est lue qu'avec `db`,
    datée d'avant la convention et vers un agent du registre (#3111).
    L'instant sert au journal des tours, jamais à une proximité arbitraire."""
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
    actor = _agent_of_email(db, email, stamp)
    if not actor:
        return "", 0.0
    return actor, stamp


def commits_written_by(repo: str, commits: Sequence[str], name: str,
                       db=None) -> Optional[bool]:
    """Un commit de la plage porte `name` comme **auteur Git** (`%ae`).

    L'auteur Git est la signature du contenu ; le committer (`commit_actor`)
    est l'agent qui a créé l'objet. Un amend ou un rebase conserve l'auteur
    d'origine et change le committer : les deux ont écrit la plage (#2808).
    Le nom Git seul ne prouve rien : l'adresse `<nom>@ameesh.invalid` est
    exigée, comme pour le committer, ou l'ancienne `<nom>@ameesh.local` datée
    (`%at`) d'avant la convention, vers un agent du registre (#3111)."""
    if not repo or not name:
        return False
    shas = [str(sha).strip() for sha in commits or () if str(sha).strip()]
    if not shas:
        return False
    text = plan_git._out(repo, "log", "--no-walk", "--format=%H%x1f%ae%x1f%at", *shas)
    if text is None or len(str(text).splitlines()) != len(set(shas)):
        return None                     # unreadable is not proof of absence
    wanted = str(name).strip().lower()
    for line in str(text).splitlines():
        fields = line.split("\x1f")
        if len(fields) != 3:
            return None
        try:
            stamp = float(fields[2].strip())
        except ValueError:
            return None
        if _agent_of_email(db, fields[1], stamp) == wanted:
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

    #3318 (B3) : la ref distante se lit pleinement qualifiée
    (`refs/remotes/origin/main`) : le nom court peut être masqué par une
    branche ou un tag local homonyme. Une cible `refs/...` se lit exactement."""
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


def _path_absent(repo: str, head: str, path: str, cache: dict):
    """Le chemin est-il absent de la tête ? None si la lecture est impossible.

    #3220 B2 : `_head_lines` rend None pour un chemin absent comme pour un
    blob illisible. `git ls-tree` distingue les deux : une sortie vide (rc 0)
    prouve l'absence, une lecture impossible rend None. La preuve de retrait
    ne retire l'auteur que sur une absence prouvée ; le doute garde."""
    key = ("absent", head, path)
    if key not in cache:
        text = plan_git._out(repo, "ls-tree", "--name-only", head, "--", path)
        cache[key] = None if text is None else not str(text).strip()
    return cache[key]


def _renamed_targets(repo: str, head: str, sha: str, cache: dict):
    """Les renommages `sha..head` (`{ancien: nouveau}`), None si illisible.

    #3220 B5 (revue cx2) : un chemin absent de la tête peut avoir été
    **déplacé** ; le retrait ou la copie qui y vivait subsiste alors au
    nouveau chemin. `git diff --name-status -z -M` donne la correspondance
    exacte ; une lecture impossible est un doute (None), jamais une absence."""
    key = ("renames", sha, head)
    if key not in cache:
        text = plan_git._out(repo, "diff", "--name-status", "-z", "-M",
                             sha, head)
        if text is None:
            cache[key] = None
        else:
            renames = {}
            fields = str(text).split("\0")
            at = 0
            while at < len(fields):
                status = fields[at]
                if not status:
                    at += 1
                    continue
                if status[0] in ("R", "C"):
                    if at + 2 >= len(fields):
                        renames = None
                        break
                    if status[0] == "R" and fields[at + 1] and fields[at + 2]:
                        renames[fields[at + 1]] = fields[at + 2]
                    at += 3
                else:
                    at += 2
            cache[key] = renames
    return cache[key]


def _renamed_target(repo: str, head: str, sha: str, path: str, cache: dict):
    """Le nouveau chemin de `path`, "" s'il n'a pas été renommé, None si la
    lecture des renommages est impossible (#3220 B5)."""
    renames = _renamed_targets(repo, head, sha, cache)
    if renames is None:
        return None
    return renames.get(path, "")


def _tree_blobs(repo: str, rev: str, cache: dict):
    """`{blob: {chemins}}` de l'arbre de `rev`, None si la lecture échoue.

    #3220 B5 (2e témoin cx2) : deux chemins peuvent porter le **même contenu**
    sans que Git ne classe le déplacement en renommage, notamment quand le
    chemin de reprise existe déjà dans la base (Git voit alors `D` + `M`)."""
    key = ("tree-blobs", rev)
    if key not in cache:
        try:
            proc = plan_git._git(repo, "ls-tree", "-r", "-z", rev)
        except Exception:  # noqa: BLE001 - lecture impossible : doute
            proc = None
        if proc is None or proc.returncode != 0:
            cache[key] = None
        else:
            blobs: dict = {}
            text = proc.stdout.decode("utf-8", "replace")
            for entry in text.split("\0"):
                if not entry:
                    continue
                meta, sep, path = entry.partition("\t")
                fields = meta.split()
                if not sep or len(fields) < 3 or not path:
                    continue
                blobs.setdefault(fields[2], set()).add(path)
            cache[key] = blobs
    return cache[key]


def _content_carried(repo: str, head: str, sha: str, path: str,
                     cache: dict):
    """Le contenu corrigé de `path` vit-il à un **autre** chemin de la tête ?

    #3220 B5 (2e témoin cx2) : `_renamed_target` ne couvre que les renommages
    classés `R` par Git. Quand le contenu corrigé est repris dans un fichier
    déjà présent, Git voit `D <path>` + `M <chemin>` et aucun `R` ; le retrait
    ou la copie a pourtant survécu. La preuve est le blob identique
    (`sha:path` == `head:autre`), pas le statut du diff. None si la lecture
    est impossible : le doute conserve l'auteur."""
    key = ("carried", sha, head, path)
    if key not in cache:
        blob = plan_git._out(repo, "rev-parse", "%s:%s" % (sha, path))
        if blob is None:
            cache[key] = None
        else:
            blobs = _tree_blobs(repo, head, cache)
            if blobs is None:
                cache[key] = None
            else:
                cache[key] = any(
                    other != path for other in blobs.get(str(blob).strip(), ()))
    return cache[key]


def _post_blob_in_head(repo: str, head: str, sha: str, path: str,
                       cache: dict):
    """Le blob post-correction de `path` vit-il **quelque part** dans la tête ?

    #3220/mesh-design : la preuve positive est le contenu, pas le chemin. Un
    fichier peut avoir été renommé (`R`), repris dans un chemin déjà présent
    (`D` + `M`, aucun `R`) ou regroupé ailleurs ; dans tous ces cas le blob
    `sha:path` est identique à un blob de la tête. None si la lecture échoue."""
    key = ("post-blob", sha, head, path)
    if key not in cache:
        blob = plan_git._out(repo, "rev-parse", "%s:%s" % (sha, path))
        if blob is None:
            cache[key] = None
        else:
            blobs = _tree_blobs(repo, head, cache)
            cache[key] = (None if blobs is None
                          else bool(blobs.get(str(blob).strip())))
    return cache[key]


def _head_file_lines(repo: str, head: str, path: str, cache: dict):
    """Les lignes (dépouillées) de `head:path`, None si le chemin/le blob est
    illisible. Un fichier vide rend une liste vide, pas None."""
    key = ("file-lines", head, path)
    if key not in cache:
        text = plan_git._out(repo, "show", "%s:%s" % (head, path))
        cache[key] = (None if text is None
                      else [line.strip() for line in str(text).splitlines()])
    return cache[key]


def _changed_paths(repo: str, head: str, sha: str, cache: dict):
    """Les chemins A/M/R/C de `sha..head`, None si la lecture échoue.

    #3220/mesh-design : la recherche de bloc reste bornée à ces chemins (et au
    chemin d'origine). `D` est exclu : il ne porte plus de contenu."""
    key = ("changed-paths", sha, head)
    if key not in cache:
        text = plan_git._out(repo, "diff", "--name-status", "-z", "-M",
                             sha, head)
        if text is None:
            cache[key] = None
        else:
            paths: set = set()
            fields = str(text).split("\0")
            at = 0
            while at < len(fields):
                status = fields[at]
                if not status:
                    at += 1
                    continue
                if status[0] in ("R", "C"):
                    if at + 2 >= len(fields):
                        paths = None
                        break
                    paths.add(fields[at + 1])
                    paths.add(fields[at + 2])
                    at += 3
                else:
                    if status[0] in ("A", "M") and at + 1 < len(fields):
                        paths.add(fields[at + 1])
                    at += 2
            cache[key] = paths
    return cache[key]


def _removal_hunks(repo: str, sha: str, path: str, cache: dict):
    """Les hunks de `path` qui **retirent** des lignes, None si illisible.

    Chaque hunk porte sa post-image (`lignes de contexte + ajouts`, la ligne
    retirée absente), sa pré-image (`lignes de contexte + lignes retirées`,
    les ajouts absents, lue depuis `sha^`), les lignes retirées et les lignes
    ajoutées. La post-image d'un retrait est le contexte qui l'entoure ; elle
    prouve la survie quand on la retrouve ailleurs d'un seul tenant. La
    pré-image prouve au contraire que le retrait a été annulé. `added`
    distingue un retrait pur du côté retiré d'une modification (#2994)."""
    key = ("removal-hunks", sha, path)
    if key not in cache:
        parent = plan_git._out(repo, "rev-parse", "--verify", "--quiet",
                               str(sha) + "^")
        if parent is None:
            cache[key] = None
        else:
            text = plan_git._out(
                repo, "diff", "--no-renames", "--no-color", "--unified=3",
                str(sha) + "^", str(sha), "--", path)
            if text is None:
                cache[key] = None
            else:
                blocks = []
                pre: list = []
                post: list = []
                removed: list = []
                added: list = []
                for line in str(text).splitlines() + ["@@"]:
                    if line.startswith("@@"):
                        if post and removed:
                            blocks.append({"pre": pre, "post": post,
                                           "removed": removed,
                                           "added": added})
                        pre, post, removed, added = [], [], [], []
                    elif line[:1] == " ":
                        value = line[1:].strip()
                        pre.append(value)
                        post.append(value)
                    elif line.startswith("+") and not line.startswith("+++ "):
                        post.append(line[1:].strip())
                        added.append(line[1:].strip())
                    elif line.startswith("-") and not line.startswith("--- "):
                        value = line[1:].strip()
                        pre.append(value)
                        removed.append(value)
                cache[key] = blocks
    return cache[key]


def _removal_block_survives(repo: str, head: str, sha: str, paths,
                            cache: dict, allow=None) -> bool:
    """Un retrait du commit survit-il par sa post-image ailleurs dans la tête ?

    #3220/mesh-design (2) : quand le blob exact a changé (contenu regroupé avec
    d'autres lignes), le bloc de contexte autour du retrait reste une preuve
    positive s'il se retrouve d'un seul tenant à un chemin de la tête et que
    la ligne retirée n'y est pas. Un bloc de moins de trois lignes
    significatives ne prouve rien (leçon #2994 : une ligne isolée rouvre le
    faux survivant) ; un chemin présent mais illisible est un doute. `allow`
    filtre les retraits qui peuvent encore prouver une survie (#2994/#2614) :
    un bloc dont un retrait ne passe pas est écarté en entier.

    #3220 B11 (arbitrage mesh-design, 24/10 00:15) : la détection
    d'annulation d'un retrait est supprimée. Un retrait effectif par rapport à
    la base de fusion et pur (`allow`) est tenu pour conservé, même si la tête
    a réintroduit les lignes : un correctif annulé garde son auteur. C'est une
    sur-rétention acceptée, dans le sens sûr ; B9, B10 et B11 venaient tous de
    ce chemin."""
    changed = _changed_paths(repo, head, sha, cache)
    if changed is None:
        return True
    for path in sorted(paths):
        blocks = _removal_hunks(repo, sha, path, cache)
        if blocks is None:
            return True
        for block in blocks:
            post = block["post"]
            gone = [row for row in block["removed"] if _meaningful_line(row)]
            if len([row for row in post if _meaningful_line(row)]) < 3:
                continue
            if not gone:
                continue
            if allow is not None and not all(allow(path, row) for row in gone):
                continue
            for candidate in sorted(set(changed) | {path}):
                now = _head_file_lines(repo, head, candidate, cache)
                if now is None:
                    absent = _path_absent(repo, head, candidate, cache)
                    if absent is None or not absent:
                        return True
                    continue
                size = len(post)
                if not any(now[at:at + size] == post
                           for at in range(len(now) - size + 1)):
                    continue
                return True
    return False


def _ordered_contains(now: Sequence[str], wanted: Sequence[str]) -> bool:
    """`wanted` apparaît-il dans `now`, dans le même ordre, lignes intercalées
    tolérées ? Les lignes vides ou triviales des deux côtés sont ignorées : un
    regroupement peut ajouter une ligne au milieu du contenu corrigé sans le
    réécrire."""
    at = 0
    for line in now:
        if at >= len(wanted):
            return True
        if not _meaningful_line(line):
            continue
        if line == wanted[at]:
            at += 1
    return at >= len(wanted)


def _source_paths(repo: str, rev: str, lines: Sequence[str], cache: dict):
    """Les chemins de `rev` qui portent déjà une des lignes copiées.

    #3220 B8 / #2977 B1 : une copie n'est prouvée que dans un chemin qui ne
    portait pas déjà la ligne avant le correctif ; sinon l'original inchangé
    (ou modifié par la tête pour une autre raison) ferait survivre une copie
    retirée. None si la lecture échoue : rien n'est alors exclu, le doute
    conserve l'auteur."""
    wanted = tuple(sorted({str(line).strip() for line in lines or ()
                           if str(line).strip()}))
    key = ("sources", rev, wanted)
    if key not in cache:
        if not wanted or not repo or not rev:
            cache[key] = set()
        else:
            try:
                proc = plan_git._git(
                    repo, "grep", "--no-color", "-l", "-I", "-F", "-f", "-",
                    str(rev),
                    stdin=("\n".join(wanted) + "\n").encode("utf-8"))
            except Exception:  # noqa: BLE001 - lecture impossible : doute
                proc = None
            if proc is None or proc.returncode not in (0, 1):
                cache[key] = None
            elif proc.returncode == 1:
                cache[key] = set()
            else:
                prefix = str(rev) + ":"
                sources = set()
                for line in proc.stdout.decode(
                        "utf-8", "replace").splitlines():
                    if not line:
                        continue
                    path = line[len(prefix):] if line.startswith(prefix) else line
                    # `grep -l` cherche des sous-chaînes : seule une ligne
                    # entière (à l'indentation près) fait de `path` une source.
                    text = _head_file_lines(repo, rev, path, cache)
                    if text is not None and any(row in text for row in wanted):
                        sources.add(path)
                cache[key] = sources
    return cache[key]


def _post_image_carried(repo: str, head: str, sha: str, path: str,
                        cache: dict, exclude=(), allow_removal=None):
    """Le contenu **complet** corrigé de `path` vit-il dans un chemin de la
    tête que le correctif (ou la tête après lui) a modifié, même enrichi ?

    #3220 B8 (revue cx2) : l'égalité stricte des blobs ne couvre pas un
    fichier corrigé repris dans un chemin déjà présent puis enrichi d'une
    ligne (`relocated/gate.py` = contenu corrigé + audit). La preuve est le
    contenu du fichier corrigé, ligne significative par ligne significative,
    retrouvé dans un chemin A/M/R/C de `sha^..head`, **sans** les lignes que
    le correctif retirait. Les chemins inchangés depuis le parent du correctif
    ne prouvent rien : une copie retirée laisse son original en place
    (#2977 B1), et une en-tête héritée ne rouvre pas #3216. `exclude` écarte
    en plus les chemins qui portaient déjà une ligne copiée : un original
    simplement modifié par la tête ne prouve pas que la copie a survécu.
    `allow_removal` filtre les retraits qui peuvent fonder la preuve quand le
    fichier n'a pas d'ajout propre : un retrait de contenu que la base de
    fusion n'a jamais porté ne prouve rien (#2994/#2614).

    Quand la post-image est petite, ce repli la traite comme un doute
    favorable à l'auteur plutôt que d'exiger trois lignes de contexte : c'est
    le contenu entier du fichier corrigé qui est comparé, pas un bloc isolé.
    #3220 B11 (mesh-design) : plus de détection d'annulation ; le contenu
    corrigé retrouvé dans un candidat suffit, même si la tête a réintroduit
    les lignes retirées. Un correctif annulé garde son auteur (sur-rétention
    acceptée).
    None si une lecture échoue (le doute conserve l'auteur)."""
    excluded = tuple(sorted(str(value) for value in exclude or ()))
    key = ("post-image", sha, head, path, excluded, allow_removal is not None)
    if key not in cache:
        current = _head_file_lines(repo, sha, path, cache)
        blocks = _removal_hunks(repo, sha, path, cache)
        parent = plan_git._out(repo, "rev-parse", "--verify", "--quiet",
                               str(sha) + "^")
        if current is None or blocks is None or parent is None:
            cache[key] = None
        else:
            wanted = [row for row in current if _meaningful_line(row)]
            gone = [row for block in blocks for row in block["removed"]
                    if _meaningful_line(row)]
            if (allow_removal is not None
                    and not all(allow_removal(path, row) for row in gone)):
                cache[key] = False
                return cache[key]
            changed = _changed_paths(repo, head, str(parent).strip(), cache)
            if changed is None:
                cache[key] = None
            elif not wanted:
                cache[key] = False
            else:
                cache[key] = False
                for candidate in sorted(changed):
                    if candidate in excluded:
                        continue             # original de la ligne copiée
                    now = _head_file_lines(repo, head, candidate, cache)
                    if now is None:
                        absent = _path_absent(repo, head, candidate, cache)
                        if absent is None or not absent:
                            cache[key] = True   # lecture impossible : doute
                            break
                        continue
                    if _ordered_contains(now, wanted):
                        cache[key] = True
                        break
    return cache[key]


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


def _raw_out(repo: str, *args: str) -> Optional[str]:
    """La sortie de git **telle quelle**, ou None en cas d'échec.

    `plan_git._out` dépouille la sortie : le blanc initial de la première
    ligne d'un fichier disparaît, et un hunk indenté ne s'y retrouve plus
    (#2994 B1 cx2/impl-04 sur e127e8d4)."""
    proc = plan_git._git(repo, *args)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def _hunks_survive(repo: str, head: str, sha: str, path: str,
                   cache: dict) -> bool:
    """Un hunk de `sha` dans `path` subsiste-t-il **tel quel** dans la tête ?

    Un réordre ou une réindentation ne change ni les lignes dépouillées ni
    leurs comptes : sa trace est la version du hunk après le commit (ses
    lignes ajoutées et une ligne de contexte de part et d'autre, indentation
    comprise). Le hunk subsiste si la tête la contient d'un seul tenant. Un
    diff ou un chemin illisible est un doute : l'auteur est conservé (#2994)."""
    key = ("hunks", sha, path)
    if key not in cache:
        text = _raw_out(repo, "diff", "--no-renames", "--no-color",
                        "--unified=1", str(sha) + "^", str(sha), "--", path)
        blocks = None
        if text is not None:
            blocks, block, added = [], [], False
            for line in str(text).splitlines() + ["@@"]:
                if line.startswith("@@"):
                    if block and added:
                        blocks.append(block)
                    block, added = [], False
                elif line[:1] in (" ", "+") and not line.startswith("+++ "):
                    block.append(line[1:].rstrip())
                    added = added or line.startswith("+")
        cache[key] = blocks
    blocks = cache[key]
    text = _raw_out(repo, "show", "%s:%s" % (head, path))
    if blocks is None or text is None:
        return True
    now = [line.rstrip() for line in str(text).splitlines()]
    return any(now[at:at + len(block)] == block
               for block in blocks
               for at in range(len(now) - len(block) + 1))


def _unique_positions(repo: str, rev: str, path: str, cache: dict):
    """`{ligne dépouillée: position}` des lignes significatives qui
    n'apparaissent qu'une fois dans `path` à `rev`, ou None si illisible."""
    key = ("unique", rev, path)
    if key not in cache:
        text = _raw_out(repo, "show", "%s:%s" % (rev, path))
        positions = None
        if text is not None:
            counts: dict = {}
            first: dict = {}
            for at, line in enumerate(str(text).splitlines()):
                value = line.strip()
                counts[value] = counts.get(value, 0) + 1
                first.setdefault(value, at)
            positions = {value: at for value, at in first.items()
                         if counts[value] == 1 and _meaningful_line(value)}
        cache[key] = positions
    return cache[key]


def _order_survives(repo: str, head: str, sha: str, path: str,
                    moved: Sequence[str], cache: dict) -> Optional[bool]:
    """Le réordre de `sha` dans `path` subsiste-t-il dans l'ordre de la tête ?

    #3003 : le hunk tel quel (`_hunks_survive`) exige ses lignes d'un seul
    tenant ; une ligne insérée par la tête entre deux lignes réordonnées le
    défait alors que le réordre subsiste. Ici, seul l'ordre relatif compte :
    pour chaque ligne déplacée, les paires dont le commit a inversé l'ordre
    (parent → commit) ; le réordre subsiste si la tête garde l'ordre du commit
    pour l'une d'elles. Seules les lignes significatives présentes une seule
    fois dans le parent, le commit et la tête ont une position sûre. None :
    rien à décider (chemin illisible, ligne répétée ou absente, aucune paire
    inversée) — le hunk tel quel reste alors la preuve."""
    before = _unique_positions(repo, str(sha) + "^", path, cache)
    after = _unique_positions(repo, str(sha), path, cache)
    now = _unique_positions(repo, head, path, cache)
    if before is None or after is None or now is None:
        return None
    decided = None
    for line in {str(row).strip() for row in moved}:
        if line not in before or line not in after or line not in now:
            continue
        for other, at in after.items():
            if other == line or other not in before or other not in now:
                continue
            ahead = after[line] < at
            if (before[line] < before[other]) == ahead:
                continue                    # paire que le commit n'inverse pas
            if (now[line] < now[other]) == ahead:
                return True
            decided = False
    return decided


def _any_line_in_head(repo: str, head: str, lines: Sequence[str]) -> bool:
    """Une des lignes apparaît-elle **quelque part** dans l'arbre de la tête ?

    Utilisé seulement quand le chemin d'origine ne porte plus la ligne : un
    fichier peut avoir été déplacé ou son contenu regroupé ailleurs (#2924
    B8). Une lecture impossible est un doute, jamais une preuve de réécriture.

    #2994 : seule une ligne **égale** à la ligne cherchée (à l'indentation
    près) compte, comme dans `_lines_in_tree` ; une ligne plus longue qui la
    contient (`self.assertIsNone(self.pick(worker), …)` pour
    `self.assertIsNone(self.pick(worker),`) n'en est pas la trace. Un fichier
    binaire est lu comme du texte : la ligne propre qu'il porte reste une
    preuve, comme avant l'égalité de ligne."""
    found = _tree_lines(repo, head, lines, binary=True)
    return True if found is None else bool(found)


def _tree_lines(repo: str, rev: str, lines: Sequence[str], *,
                binary: bool = False) -> Optional[set]:
    """Les lignes (dépouillées) de `lines` égales à une ligne de l'arbre de
    `rev` (à l'indentation près), ou None si la lecture est impossible.

    `git grep -F` trouve les lignes qui *contiennent* un motif ; seules celles
    qui lui sont égales une fois dépouillées sont rendues. Le format de sortie
    est fixé (ni couleur, ni numéro, ni colonne) : la configuration Git du
    dépôt ou de l'hôte (`grep.lineNumber`, `color.grep=always`…) ne doit pas
    changer la lecture (#2994 B1 cx1). `binary=True` lit aussi les fichiers
    binaires comme du texte (`-a`) ; sinon ils sont ignorés (`-I`)."""
    wanted = {str(line).strip() for line in lines or () if str(line).strip()}
    if not wanted:
        return set()
    if not repo or not rev:
        return None
    try:
        proc = plan_git._git(repo, "grep", "--no-color", "--no-line-number",
                             "--no-column", "-h", "-a" if binary else "-I",
                             "-F", "-f", "-", rev,
                             stdin=("\n".join(sorted(wanted)) + "\n").encode("utf-8"))
    except Exception:  # noqa: BLE001 - lecture impossible : doute
        return None
    if proc.returncode == 1:
        return set()
    if proc.returncode != 0:
        return None
    found = {line.strip() for line in
             proc.stdout.decode("utf-8", "replace").splitlines()}
    return wanted & found


def _lines_in_tree(repo: str, rev: str, lines: Sequence[str]) -> set:
    """Les lignes (dépouillées) de `lines` présentes **telles quelles** dans
    l'arbre de `rev`, à l'indentation près.

    Seules les lignes égales comptent (`_tree_lines`), pour qu'une ligne propre
    au correcteur ne soit jamais écartée parce qu'une ligne plus longue de la
    base la contient. Une lecture impossible rend l'ensemble vide : rien n'est
    écarté, le doute conserve l'auteur (#2977)."""
    return _tree_lines(repo, rev, lines) or set()


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


def _removal_proves_survival(repo: str, head: str, sha: str, path: str,
                             row: str, base: str,
                             cache: dict) -> bool:
    """Un retrait est-il **effectif** et **pur**, donc tenu pour conservé ?

    mesh-design (arbitrage B11) : un retrait effectif par rapport à la base de
    fusion (la ligne y existait au même chemin, ou au chemin renommé `R`) et
    pur (son hunk n'ajoute aucune ligne) est tenu pour conservé, même si la
    tête a réintroduit les lignes ; c'est une sur-rétention acceptée, dans le
    sens sûr. #2994/#2614 : un retrait vacant (la base ne l'a jamais porté) ou
    le côté retiré d'une modification (le hunk ajoute une ligne, même héritée :
    un réordre n'est pas un retrait pur) ne prouve rien. Le retrait se juge
    hunk par hunk (B13) : un hunk pur suffit, même si un autre hunk, séparé
    par du contexte, retire le même texte dans une modification. Une ligne
    ajoutée **triviale** (vide, `pass`, `}`) ne fait pas une modification : un
    hunk qui ne retire que des lignes reste pur (B14). Une lecture impossible
    reste un doute : le retrait compte (l'auteur est conservé)."""
    if not base:
        return True
    key = ("merge-base", head, base)
    if key not in cache:
        cache[key] = plan_git._out(repo, "merge-base", head, base) or ""
    merge_base = cache[key]
    if not merge_base:
        return True                     # base illisible : doute
    before = _path_line_counts(repo, merge_base, path, cache)
    if before is None:
        if _path_absent(repo, merge_base, path, cache) is None:
            return True                 # lecture impossible : doute
        before = {}                     # chemin absent : peut être renommé
    if row.strip() not in before:
        # mesh-design : le chemin de la base peut être l'ancien nom d'un `R`.
        renames = _renamed_targets(repo, sha, merge_base, cache)
        if renames is None:
            return True                 # renommages illisibles : doute
        sources = [old for old, new in renames.items() if new == path]
        if not sources:
            return False                # la base ne l'a jamais portée
        for source in sources:
            before = _path_line_counts(repo, merge_base, source, cache)
            if before is None:
                return True             # lecture impossible : doute
            if row.strip() in before:
                break
        else:
            return False
    blocks = _removal_hunks(repo, sha, path, cache)
    if blocks is None:
        return True
    mixed = False
    for block in blocks:
        if row.strip() not in {value.strip() for value in block["removed"]}:
            continue
        if not any(_meaningful_line(value)
                   for value in block.get("added") or ()):
            return True                 # un hunk pur suffit (B13/B14)
        mixed = True
    return not mixed


def _commit_survives(repo: str, head: str, sha: str, cache: dict,
                     base: str = "") -> bool:
    """Une partie significative du commit subsiste-t-elle dans la tête ?

    C'est la preuve de contenu de #2924, volontairement **prudente** : un
    commit n'est tenu pour réécrit que si **aucune** de ses lignes
    significatives ne subsiste. Une ligne ou un commentaire modifié, une
    réorganisation partielle ne suffisent donc jamais à retirer son auteur
    (canon §2 : le doute conserve l'auteur) ; une réécriture complète, elle,
    ne le reprend pas. Un diff illisible ou binaire conserve l'auteur.

    #2924 B8 : un chemin absent ne prouve pas à lui seul une suppression. Un
    fichier peut avoir été déplacé (ou son contenu regroupé ailleurs) sans
    qu'une ligne soit réécrite ; la preuve cherche donc les lignes
    significatives du commit dans tout l'arbre de la tête, pas seulement à
    leur ancien chemin.

    #3220 : si aucune de ces lignes ne subsiste nulle part, le contenu n'a pas
    forcément disparu : la lecture examine encore les lignes retirées. Un
    retrait **effectif** par rapport à la base de fusion et **pur** garde
    l'auteur, même si la tête a réintroduit la ligne au même chemin (#3220
    B12, arbitrage mesh-design : sur-rétention acceptée) ; la présence de la
    ligne ne défait pas le retrait. Sinon, un retrait conservé (la ligne n'est
    plus dans la tête au chemin d'origine) garde l'auteur, sauf quand le commit
    a des additions propres qui ont toutes disparu et que le chemin était
    encore présent après le commit : une suppression ultérieure a pu emporter
    l'édition entière (#3220 B2, revue dev2). Les copies se lisent ensuite : une copie encore à son chemin, avec
    une occurrence de plus que dans le parent, garde l'auteur (#2977) ; une
    copie à un chemin prouvé absent ne le garde pas dans ce même cas, sinon une
    ligne d'en-tête commune rouvrirait #3216 (revue mesh-design). Quand des
    additions propres ont toutes disparu, le simple compte ne suffit plus : le
    contenu entier ou le déplacement doit confirmer la copie (#2994/#2614).
    Sans addition propre, un chemin absent reste le doute B8. Une lecture
    impossible reste un doute (`_any_line_in_head` rend True), jamais une
    preuve de suppression.

    #3220 B8 (revue cx2) : quand même le blob exact du fichier corrigé a
    disparu, son contenu complet peut avoir été repris dans un chemin déjà
    présent puis enrichi d'une ligne (aucun `R`). `_post_image_carried` le
    cherche, ligne significative par ligne significative, dans les chemins
    modifiés depuis le parent du correctif, en exigeant l'absence des lignes
    retirées. Pour un petit fichier, ce repli est un doute favorable à l'auteur
    (le contenu entier est comparé), pas un bloc de contexte isolé.

    #2994 (cas #2614, revue dev7) : une ligne retirée puis réajoutée dans le
    **même** fichier n'est pas un déplacement, elle est héritée ; et quand les
    ajouts propres ont tous disparu, un retrait ne prouve la survie que s'il
    portait sur une ligne déjà dans la base de fusion au même chemin, et s'il
    n'est pas le côté retiré d'une modification dont les ajouts ont disparu.
    Un réordre ne subsiste que si son hunk est intact dans la tête, ou
    l'ordre d'une paire de lignes qu'il inverse, même séparées par une
    ligne insérée (#3003).

    #2977 : une ligne ajoutée qui existait déjà avant le correctif (parent du
    commit, ou base de fusion de la tête et de `base`) n'est pas propre au
    correcteur et ne prouve rien ; seules ses lignes propres sont cherchées,
    avec la même prudence (chemin d'origine, puis tout l'arbre, B8). Une ligne
    que le commit retire aussi ailleurs (un déplacement) reste une preuve,
    comme avant. Sans ligne propre, la lecture passe aux lignes retirées,
    puis aux lignes copiées, cherchées à leur seul chemin (un chemin absent
    reste un doute, sauf le cas #3220 B2 ci-dessus) : une copie retirée ne
    garde pas son correcteur (B1). Un diff sans ligne significative conserve
    l'auteur."""
    parsed = _commit_lines(repo, sha)
    if parsed is None:
        return True
    adds, removes, binary = parsed
    if binary:
        return True
    added = [(path, row) for path, rows in adds.items() for row in rows
             if _meaningful_line(row)]
    copied: list = []
    removed_from: dict = {}
    if added:
        for path, rows in removes.items():
            for row in rows:
                removed_from.setdefault(row.strip(), set()).add(path)

        def moved(path, row) -> bool:
            # #2994 : seul un déplacement **entre chemins** est une preuve de
            # l'écrit de l'acteur ; une ligne retirée puis réajoutée dans le
            # même fichier (réordre, réindentation) reste héritée si elle
            # existait déjà avant le commit.
            return bool(removed_from.get(row.strip(), set()) - {path})

        inherited = _inherited_lines(
            repo, head, sha, base,
            [row for path, row in added if not moved(path, row)], cache)
        copied = [(path, row) for path, row in added
                  if not moved(path, row) and row.strip() in inherited]
        added = [(path, row) for path, row in added
                 if moved(path, row) or row.strip() not in inherited]
    if added:
        for path, row in added:
            lines = _head_lines(repo, head, path, cache)
            if lines is not None and row.strip() in lines:
                return True
        # Le chemin d'origine ne porte plus la ligne : elle a pu être déplacée
        # avec son fichier ou regroupée ailleurs. La chercher dans tout l'arbre
        # évite d'effacer le correcteur sur un simple déplacement (#2924 B8).
        if _any_line_in_head(repo, head, [row for _path, row in added]):
            return True
        # #3220 B1 (revue cx2) : des additions propres ont disparu, mais le
        # commit peut encore avoir retiré des lignes dont le retrait subsiste.
        # La conclusion « contenu disparu » attend ce contrôle.
    removed = [(path, row) for path, rows in removes.items() for row in rows
               if _meaningful_line(row)]
    # #3220/mesh-design (1) : preuve positive par le contenu, indépendante du
    # chemin. Un fichier touché par une contribution **propre** (ajout non
    # hérité ou retrait) dont le blob post-correction existe tel quel quelque
    # part dans la tête a survécu : renommage `R`, `D` + `M` sans `R`,
    # regroupement. Les copies pures (que des lignes héritées) sont exclues :
    # leur blob existe déjà avant le correctif et ne prouve rien (#2977 B1).
    own_paths = {path for path, _row in added}
    own_paths.update(path for path, _row in removed)
    for path in sorted(own_paths):
        if _path_absent(repo, sha, path, cache):
            continue                 # supprimé par le commit : doute B4
        own_add_here = any(other == path for other, _row in added)
        path_removed = [row for other, row in removed if other == path]
        allow_removal = None
        if not own_add_here:
            allow_removal = (lambda block_path, block_row:
                             _removal_proves_survival(
                                 repo, head, sha, block_path, block_row,
                                 base, cache))
            if path_removed and not all(
                    allow_removal(path, row) for row in path_removed):
                # #2994/#2614 : le contenu du fichier peut être celui de la
                # base ; sans ajout propre, un retrait que la base de fusion
                # n'a jamais porté ne fonde aucune preuve.
                continue
        present = _post_blob_in_head(repo, head, sha, path, cache)
        if present is None or present:
            return True
        # #3220 B8 (revue cx2) : le blob exact peut avoir été enrichi d'une
        # ligne (regroupement partiel). Le contenu complet du fichier corrigé,
        # retrouvé dans un chemin modifié depuis le parent du correctif, reste
        # une preuve ; les chemins inchangés ne prouvent rien (#2977 B1).
        carried = _post_image_carried(repo, head, sha, path, cache,
                                      allow_removal=allow_removal)
        if carried is None or carried:
            return True
    if removed:
        for path, row in removed:
            lines = _head_lines(repo, head, path, cache)
            if lines is None:
                # #3220 B2 (revues dev2 + cx2) : quand les additions propres
                # du commit ont toutes disparu, un chemin absent ne vaut
                # preuve que si le commit ne l'a pas supprimé lui-même. S'il
                # existait après le commit, une suppression ultérieure a pu
                # emporter l'édition entière ; une lecture impossible reste
                # un doute.
                if added:
                    absent_head = _path_absent(repo, head, path, cache)
                    absent_after = _path_absent(repo, sha, path, cache)
                    if absent_head is None or absent_after is None:
                        return True
                    if absent_head and not absent_after:
                        # #3220 B5 (revue cx2) : le fichier corrigé a pu être
                        # déplacé ; le retrait subsiste alors au nouveau
                        # chemin. Sans renommage prouvé, on ne conclut pas.
                        moved = _renamed_target(repo, head, sha, path, cache)
                        if moved is None:
                            return True
                        if moved:
                            target = _head_lines(repo, head, moved, cache)
                            if target is None or row.strip() not in target:
                                return True
                        else:
                            # #3220 B5 (2e témoin cx2) : pas de `R`, mais le
                            # contenu corrigé peut avoir été repris tel quel
                            # dans un fichier déjà présent (regroupement B8).
                            carried = _content_carried(
                                repo, head, sha, path, cache)
                            if carried is None or carried:
                                return True
                            # #3220 B8 (revue cx2) : le contenu corrigé a pu
                            # être repris dans un chemin existant **puis
                            # enrichi** (aucun blob exact, aucun `R`). Le
                            # fichier entier corrigé, retrouvé dans un chemin
                            # modifié depuis le parent, garde le retrait.
                            partial = _post_image_carried(
                                repo, head, sha, path, cache)
                            if partial is None or partial:
                                return True
                        continue
                return True
            if row.strip() not in lines:
                # #2994/#2614 (revue dev7) : quand les ajouts propres du
                # commit ont tous disparu, un retrait ne prouve la survie que
                # s'il portait sur une ligne déjà dans la base de fusion au
                # même chemin, et si c'est un retrait pur (pas le côté retiré
                # d'une modification). Sinon la tête, repartie de la base, n'a
                # jamais reçu cette ligne.
                if added and not _removal_proves_survival(
                        repo, head, sha, path, row, base, cache):
                    continue
                return True
            # B12 (revues auditeur + cx3) : la ligne retirée est revenue **au
            # même chemin** (tête repartie de la base, correctif non rejoué).
            # mesh-design (arbitrage B11) : un retrait effectif par rapport à
            # la base de fusion et pur est tenu pour conservé, même annulé ;
            # la présence de la ligne ne le défait pas. Sans cette lecture, la
            # boucle laissait tomber le retrait et la fin rendait False.
            if _removal_proves_survival(
                    repo, head, sha, path, row, base, cache):
                return True
    if added:
        # #3220/mesh-design (2) : le blob exact peut avoir changé (contenu
        # regroupé avec d'autres lignes) ; la post-image d'un retrait retrouvée
        # d'un seul tenant ailleurs, sans la ligne retirée, prouve sa survie.
        # #2994/#2614 : un bloc dont un retrait ne satisfait pas la règle de
        # survie (base de fusion, retrait pur) est écarté.
        # B1 (cx3/auditeur) : ce contrôle passe **avant** la conclusion des
        # copies/réordres ; sinon une en-tête héritée dans un doc fait rendre
        # `False` à la branche `copied` alors qu'un retrait survit.
        allow = (lambda block_path, block_row:
                 _removal_proves_survival(
                     repo, head, sha, block_path, block_row, base, cache))
        if _removal_block_survives(
                repo, head, sha, {path for path, _row in removed}, cache,
                allow=allow):
            return True
    if copied:
        # #2977 B1/B2 et #3220 B2/B5 (revues cx2) : une copie ne subsiste
        # qu'à son chemin, ou au chemin déplacé du fichier. Une occurrence de
        # plus que dans le parent la prouve ; un chemin absent sans renommage
        # ne la prouve pas quand des additions propres ont disparu (sinon une
        # ligne d'en-tête commune rouvrirait #3216). Sans addition propre, un
        # chemin absent reste le doute B8.
        copy_sources: dict = {}
        for path, row in copied:
            if path not in copy_sources:
                copy_sources[path] = _source_paths(
                    repo, str(sha) + "^",
                    [line for other, line in copied if other == path], cache)
            excluded = copy_sources[path] or ()
            present = _path_line_counts(repo, head, path, cache)
            if present is None:
                if added:
                    absent = _path_absent(repo, head, path, cache)
                    if absent is None:
                        return True
                    if absent:
                        moved = _renamed_target(repo, head, sha, path, cache)
                        if moved is None:
                            return True
                        if moved:
                            target = _path_line_counts(repo, head, moved, cache)
                            if target is None:
                                return True
                            before = _path_line_counts(
                                repo, str(sha) + "^", path, cache) or {}
                            if target.get(row.strip(), 0) > before.get(
                                    row.strip(), 0):
                                return True
                        else:
                            # #3220 B5 (2e témoin cx2) : la copie a pu être
                            # reprise avec le fichier corrigé dans un chemin
                            # déjà présent, sans que Git ne voie de `R`.
                            carried = _content_carried(
                                repo, head, sha, path, cache)
                            if carried is None or carried:
                                return True
                            # #3220 B8 (revue cx2) : la copie héritée vit
                            # dans le fichier corrigé repris **puis enrichi**
                            # (aucun blob exact, aucun `R`). Le contenu entier
                            # du fichier, retrouvé dans un chemin modifié
                            # depuis le parent, prouve que la copie subsiste ;
                            # l'original inchangé d'un autre chemin ne prouve
                            # rien (#2977 B1).
                            partial = _post_image_carried(
                                repo, head, sha, path, cache,
                                exclude=excluded)
                            if partial is None or partial:
                                return True
                        continue
                return True
            reordered_line = path in removed_from.get(row.strip(), set())
            before = _path_line_counts(repo, str(sha) + "^", path, cache) or {}
            # #2994 : un réordre dans le même fichier n'est pas une copie ;
            # une occurrence de plus dans la tête ne prouve pas son hunk.
            # Seul `_hunks_survive` décide pour ces lignes. Et quand des
            # additions propres ont toutes disparu, le simple compte ne dit
            # pas que la copie est la leur (cas #2614) : il faut le contenu
            # entier ou le déplacement, plus bas.
            if (not added and not reordered_line
                    and present.get(row.strip(), 0) > before.get(
                        row.strip(), 0)):
                return True
            # #3220 B8 (revue cx2) : le chemin existe encore, mais la copie a
            # pu être reprise dans un fichier voisin enrichi après le
            # correctif. Le contenu entier corrigé, retrouvé dans un chemin
            # modifié depuis le parent, la prouve sans dépendre d'un `R`.
            partial = _post_image_carried(repo, head, sha, path, cache,
                                          exclude=excluded)
            if partial is None or partial:
                return True
        # #2994 : un réordre (ou une réindentation) dans le même fichier
        # n'ajoute aucune occurrence ; sa trace est le hunk tel que le commit
        # l'a laissé, s'il subsiste dans la tête. #3003 : ou l'ordre relatif
        # de ses lignes, quand la tête a inséré une ligne au milieu.
        reordered: dict = {}
        for path, row in copied:
            if path in removed_from.get(row.strip(), set()):
                reordered.setdefault(path, []).append(row)
        if any(_order_survives(repo, head, sha, path, rows, cache)
               or _hunks_survive(repo, head, sha, path, cache)
               for path, rows in sorted(reordered.items())):
            return True
        if not added:
            return False
    if added:
        # #3220 : les additions propres du commit ont toutes disparu, et aucun
        # retrait ni copie ne subsiste.
        return False
    # Un commit sans addition significative (purs retraits, ou diff trivial) :
    # il survit tant que ses retraits ne sont pas tous annulés dans la tête.
    return not removed


def _freeze_content_preserved(repo: str, freeze_sha: str, head_sha: str,
                              base: str = "") -> bool:
    """Le contenu du gel est-il encore celui de la tête (même arbre) ?

    `True` quand l'arbre est identique, ou quand la tête est exactement la
    fusion du gel avec la base (une consolidation sur une cible avancée). Un
    arbre illisible ne prouve pas une réécriture : le doute rend `True` (les
    auteurs restent). Une fusion **conflictuelle**, elle, n'est pas une preuve
    de conservation : la fonction rend `False`, et la preuve de contenu par
    commit (#3220) décide sur la tête réelle si les lignes du gel antérieur y
    subsistent. Sans cette lecture, un rebase conflictuel garderait tous les
    auteurs d'un gel dont un fichier a pourtant disparu (#3216)."""
    head_tree = _tree_of(repo, head_sha)
    freeze_tree = _tree_of(repo, freeze_sha)
    if head_tree is None or freeze_tree is None:
        return True
    if head_tree == freeze_tree:
        return True
    if base:
        merged = _merged_tree(repo, freeze_sha, base)
        if merged is not None and merged == head_tree:
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
            actor, stamp = commit_actor(repo, sha, db)
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


def _forge_fetch(repo: str, sha: str) -> bool:
    """Rapatrie le commit exact `sha` depuis la forge du clone (`origin`).

    #3147 : la forge sert encore par SHA un commit devenu inaccessible après un
    push forcé ; un clone récent ne l'a pas dans ses objets. La lecture de
    provenance ne doit donc pas dépendre du clone : elle demande le seul commit
    exact à `origin`, sans déplacer aucune ref et sans écrire `FETCH_HEAD`.
    Un SHA abrégé ne se demande pas (Git exige l'identifiant complet), un
    dépôt sans `origin` n'a pas de forge, et un objet que la forge ne sert pas
    laisse l'absence : le refus fermé de #2924 B5 reste la règle.
    """
    text = plan_git.full_commit_id(sha)
    if not text:
        return False
    try:
        if plan_git.local_commit(repo, text):
            return True
    except Exception:  # noqa: BLE001 - dépôt illisible : aucune récupération
        return False
    try:
        if plan_git._out(repo, "remote", "get-url", "origin") is None:
            return False
        proc = plan_git._git(
            repo, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
            "--", "origin", text, timeout=plan_git.FETCH_TIMEOUT)
    except Exception:  # noqa: BLE001 - forge injoignable : absence
        return False
    if proc.returncode != 0:
        return False
    try:
        return bool(plan_git.local_commit(repo, text))
    except Exception:  # noqa: BLE001 - lecture impossible : absence
        return False


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

    #2924 B7 : `recover(sha)` est une source de réparation supplémentaire du
    # gel antérieur absent (le dépôt de l'agent, une autre origine) ; #3147
    # ajoute la forge du clone (`origin`), tentée par SHA exact **quel que soit
    # l'appelant**. Une forge qui ne sert pas l'objet laisse le refus fermé de
    # B5 : une absence non réparée ne prouve jamais l'absence d'un correcteur.

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
        """Le contenu écrit par `actor` dans ce gel subsiste-t-il dans la tête ?

        #3220 : un commit déjà présent dans la plage courante est attribué par
        la lecture de cette tête ; le re-prouver par le contenu serait
        redondant, et un gros commit gelé coûterait un `git grep` complet sur
        tout l'arbre. Seuls les commits du gel antérieur que la plage courante
        ne couvre pas demandent la preuve de contenu."""
        shas = written.get(actor) or []
        if not shas:
            return False
        if stored:
            return True
        shas = [commit for commit in shas
                if plan_git.full_commit_id(commit) not in current_set]
        if not shas:
            return True              # déjà attribué par la plage courante
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
        """Tente de rapatrier un gel absent ; True seulement s'il est lisible.

        #3147 : la forge du clone est **toujours** tentée par SHA exact, même
        sans `recover` d'appelant ; `recover` reste une source supplémentaire
        (dépôt de l'agent, autre origine) essayée d'abord quand elle est
        fournie. Un hook qui échoue ne prive pas la forge de sa tentative ;
        une forge qui ne sert pas l'objet laisse le refus fermé.
        """
        try:
            if present(commit):
                return True
        except Exception:  # noqa: BLE001 - dépôt illisible : absence
            return False
        if recover is not None:
            try:
                if recover(commit) and present(commit):
                    return True
            except Exception:  # noqa: BLE001 - source en échec : la forge reste
                pass
        try:
            return _forge_fetch(repo, commit) and present(commit)
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
    `recover(sha)` (#2924 B7) est une source supplémentaire pour rapatrier un
    gel antérieur absent du clone de lecture ; #3147 fait en sorte que la forge
    du clone (`origin`) soit **toujours** tentée par SHA exact, même sans
    `recover`. Un objet que la forge ne sert pas garde le refus fermé de B5. Un
    acteur prouvé sous plusieurs familles dans un gel
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
            actor, stamp = commit_actor(repo, sha, db)
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
            written = commits_written_by(repo, commits, author_name, db)
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
