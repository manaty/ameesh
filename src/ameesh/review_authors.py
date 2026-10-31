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
  relecteurs par son nom (#2739). Exception #3518/#3577 : une correction dont
  tous les chemins sont des tests reconnus (§11 bis), hors plan CI, garde
  seulement cette exclusion personnelle, même sous un chemin sensible ; la
  classe du lot ne change pas ;
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
  la porte ne dépend pas du clone. Un gel **déclaré remplacé** dont l'objet
  n'est plus contenu par une ref (objet pendant d'un force-push, #3399) garde
  ses auteurs connus dont le contenu survit ; seuls ses inconnus cessent de
  bloquer. Le refus fermé B5 ne vaut que pour un gel encore accessible ;
* un commit **déjà dans la cible** au moment de la lecture, par ascendance ou
  patch-id verbatim identique (#3762), ne compte ni comme
  auteur ni comme tiers du lot (#3116) : la plage reportée d'un ancien gel,
  calculée avant un rebase, peut contenir du develop déjà relu sous son propre
  lot ; un inconnu d'un gel antérieur qui n'est plus ancêtre de la tête et dont
  le contenu ne survit pas ne bloque pas non plus (#3116) ;
* un correctif de relecteur ne pèse que s'il survit dans la tête (#3335) :
  la promotion en auteur (#2888) et la trace « correcteur » suivent la même
  preuve de contenu que #3220, y compris pour un commit de la plage courante
  retiré par un commit postérieur. Une ligne qui survit suffit ; une lecture
  impossible garde l'exclusion ;
* une contribution tierce ne pèse que si elle survit dans la tête (#4179) :
  un commit hérité d'un parent empilé puis retiré (revert, annulation) n'est
  pas un contenu de cette tête et sa famille redevient éligible ; une ligne
  qui survit, ou une lecture impossible, garde l'exclusion ;
* un lot empilé sur la tête gelée d'un parent — prérequis déclaré ou membre
  du même paquet — lit `parent..tête` (#4343/#4478) : les commits du parent,
  relus sur sa propre tête, ne sont ni auteurs ni inconnus de l'enfant. La
  porte de fusion ne retire cette tête que si la **propre porte du parent**
  est satisfaite sur elle (`merge_queue._admitted_stacked_parents`) ; un
  parent jamais relu ou bloqué reste dans le périmètre de l'enfant. Le doute
  (parent, gel ou objet illisible) conserve l'auteur ;
* un ancien commit sans reçu et sans attribution ne bloque plus quand son
  contenu a disparu, ou quand le même auteur l'a réémis avec reçu exact et
  patch-id verbatim identique dans la plage courante (#3301, constat
  mesh-design du 24/10/2026 sur #3033). #4387 : la réémission, par l'**auteur
  de référence** de la tête, de la **tête d'un gel antérieur écrite par
  l'acteur de ce gel** et sortie de l'histoire (« correctif de relecteur
  réécrit par l'auteur », canon §2, **petit et sans conception**) vaut aussi,
  à patch-id verbatim identique et reçu exact ; l'acteur reste correcteur
  exclu par son nom, sa famille redevient éligible. Un autre auteur ne peut
  toujours pas officialiser le patch d'un tiers, et un commit tiers de la
  plage antérieure qui survit garde son inconnu. Cela ne crée aucun reçu
  tardif et ne retire jamais un auteur historique prouvé ;
* quand le reçu exact manque (commit restauré par rebase, correction poussée
  hors du hook), le commit est attribué à la famille de session de son
  committer, lue au journal des tours (`turn_costs`, `_turns_harness`) : un
  commit du relecteur désigné reste sa correction, un commit de l'assigné
  reste l'auteur. #3805 : quand l'auteur Git est un agent du registre
  **différent du committer**, les deux familles comptent comme auteurs du
  contenu (le doute conserve, comme #2924) ; une réémission à patch-id
  verbatim identique d'un commit qui porte un reçu exact garde ce reçu, et la
  copie n'est pas attribuée à son committer. Un tiers non relecteur sans reçu
  et sans auteur Git d'agent reste inconnu : aucun camp ne le suppose. #2721 :
  à la désignation, ce repli porte sa trace (`proof: turn-journal`,
  `fallback_commits`), et un committer distinct de la référence doit être au
  registre.

Le module ne devine jamais une famille depuis un nom Git seul : le committer
et l'auteur Git doivent porter l'adresse d'agent (`<nom>@ameesh.invalid`),
l'agent doit être au registre, et la famille vient du journal des tours. Un
reçu exact contradictoire est une erreur, pas une absence.
"""
from __future__ import annotations

import codecs
import contextlib
import contextvars
import difflib
import os
import re
import tempfile

from typing import Mapping, Optional, Sequence

from . import (canon, fetch_cache, grep_cache, plan_git, registry, review_lot,
               review_provenance, storage)
from .deadline import Deadline

#: #3974 : échéance du balayage d'auctorialité, posée par l'appelant borné
#: (`sweep_deadline`) ; sans elle, rien ne change (les veilles historiques).
_SWEEP: contextvars.ContextVar = contextvars.ContextVar(
    "review_authors_sweep", default=None)


class SweepDeadline(RuntimeError):
    """#3974 : l'échéance du balayage d'auctorialité est épuisée.

    Jamais lue comme un doute qui conclut : l'appelant borné refuse en
    nommant le lot et l'étape. Les balayages déjà faits restent dans le cache
    de l'hôte (`grep_cache`), le passage suivant repart plus loin."""


class _Sweep:
    def __init__(self, budget: float, label: str):
        self.deadline = Deadline(budget)
        self.budget = float(budget)
        self.label = str(label or "")
        self.tripped = ""              # première étape refusée à l'échéance
        self.trips = 0                 # #4469 : refus comptés, lus par lecture

    def check(self, step: str) -> None:
        if self.deadline.expired():
            self.trip(step)

    def trip(self, step: str) -> None:
        self.trips += 1
        if not self.tripped:
            self.tripped = step
        raise SweepDeadline(
            "%sthe authorship sweep exceeded %d s at step %s"
            % (self.label + ": " if self.label else "", int(self.budget), step))


@contextlib.contextmanager
def sweep_deadline(budget: float, label: str = ""):
    """Borne les balayages `git grep` de la lecture d'auctorialité (#3974).

    Chaque balayage ne prend que le temps qui reste ; une échéance épuisée
    lève `SweepDeadline`. `tripped` dit si une étape a été refusée, même si
    une couche intermédiaire a transformé l'exception en doute."""
    sweep = _Sweep(budget, label)
    token = _SWEEP.set(sweep)
    try:
        yield sweep
    finally:
        _SWEEP.reset(token)


def active_sweep():
    """L'échéance de balayage posée par l'appelant courant, ou None (#4469)."""
    return _SWEEP.get()


def _sweep_grep(repo: str, rev: str, args: Sequence[str], patterns: Sequence[str],
                step: str):
    """`git grep <args> -f - <rev>` borné par l'échéance courante, s'il y en a.

    Les attributs lus sont ceux de l'arbre de `rev` (`grep_cache`).
    Rend le processus (ou lève `SweepDeadline`) ; une autre erreur remonte
    à l'appelant, qui la lit comme un doute."""
    sweep = _SWEEP.get()
    stdin = ("\n".join(patterns) + "\n").encode("utf-8")
    # B/P2 cx2 : les attributs (`-I`) sont ceux de l'arbre balayé, jamais ceux
    # du dépôt de travail lecteur ; le résultat se partage entre clones.
    env = grep_cache.neutral_attributes(rev, repo)
    if sweep is not None:
        sweep.check(step)
    try:
        with _local_object_repo(repo) as (reader, local_env):
            proc = plan_git._git(
                reader, "grep", *args, "-f", "-", str(rev), stdin=stdin,
                timeout=sweep.deadline.remaining() if sweep is not None else canon.GIT_TIMEOUT,
                deadline=sweep.deadline if sweep is not None else None,
                env_extra={**env, **local_env})
            # Missing blobs can yield rc=1 (no matches) or rc=0 (partial
            # matches). Neither proves absence and neither may be shared.
            if proc.stderr:
                raise canon.GitError(proc.stderr.decode("utf-8", "replace"))
            return proc
    except canon.GitDeadline:
        if sweep is not None:
            sweep.trip(step)
        raise


def _cache_wait() -> Optional[float]:
    sweep = _SWEEP.get()
    return None if sweep is None else sweep.deadline.remaining()


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
    text = _read_out(repo, "show", "--name-only", "--format=", sha)
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
                     families_out: Optional[dict] = None,
                     receipt: Optional[Mapping] = None) -> dict:
    """L'auteur de référence `{name, family}` : l'assigné au gel.

    `receipt` (#3948) est la preuve déjà capturée du jalon en cours, quand la
    lecture est faite **avant** l'écriture du `frozen` : le reçu n'existe pas
    encore en base, mais `proof_for_current_turn` le connaît. Il tient lieu de
    `_milestone_receipt` sans relire la base ni retarder le gel.

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
    if receipt is not None:
        # #3948 : la lecture précède l'INSERT du jalon, son reçu n'est pas
        # encore en base. La preuve du tour en tient lieu, sans relecture.
        proof = dict(receipt)
        if proof and str(proof.get("actor") or "").strip() == name:
            prove(proof.get("harness"))
    elif milestone is not None and milestone_id > 0:
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
    row = {"name": name, "family": family}
    if len(proven) > 1:
        # #3584 : un auteur prouvé sous plusieurs familles transporte tout
        # l'ensemble ; la désignation et la porte excluent chacune d'elles.
        row["families"] = sorted(proven)
    return row


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
    text = _read_out(repo, "show", "--numstat", "-z", "--format=",
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


def _test_only_correction(repo: str, entries, project: str) -> bool:
    """All changed paths must be test adaptations (§2/§11 bis, #3518/#3577).

    Reuse the vehicle's test recognition. CI plans, unreadable or empty diffs
    and an unreadable project policy never buy an exception to the author-family
    exclusion. A sensitive path does not prevent it (#3577): the correction
    keeps its author family eligible for the product delta while the lot's
    class is unchanged. --no-renames includes both sides of a move out of
    product code.
    """
    from . import config, lot_class, packet_reviews, work

    if not repo or not entries:
        return False
    changed = set()
    if not _local_reads(repo, [sha for sha, _family in entries], {}):
        return False
    for sha, _family in entries:
        text = _read_out(repo, "show", "--format=", "--name-only", "-z",
                             "--no-renames", sha)
        if text is None:
            return False
        paths = [path for path in text.split("\0") if path]
        if not paths or any(packet_reviews._CI_PLAN.search(path)
                            or not packet_reviews._TEST.search(path)
                            for path in paths):
            return False
        changed.update(paths)
    try:
        klass = lot_class.class_of_paths(
            work.configured_canons(config.load()), project, sorted(changed))
    except Exception:  # noqa: BLE001 - unknown policy never grants an exception
        return False
    # #3577: the risk class no longer forbids the exception — a test-only
    # correction stays test-only under a sensitive path. The policy must still
    # be readable (no doubt): an unknown policy never grants an exception.
    return not klass.get("doubt")


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
    text = _read_out(repo, "show", "-s", "--format=%ce%x1f%ct", sha)
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


def commit_author(repo: str, sha: str, db=None) -> tuple[str, float]:
    """`(acteur, instant)` de l'**auteur Git** d'adresse d'agent, sinon ("", 0).

    L'auteur Git est la signature du contenu ; un rebase ou un amend conserve
    l'auteur d'origine et change le committer (#2808). Le nom Git seul ne
    prouve rien : l'adresse `<nom>@ameesh.invalid` est exigée **et l'agent doit
    être au registre** (#3805) ; l'ancienne adresse `<nom>@ameesh.local` n'est
    lue qu'avec `db`, datée d'avant la convention et vers un agent du registre
    (#3111). L'instant (`%at`) sert au journal des tours du commit d'origine,
    jamais à une proximité arbitraire.

    Une lecture impossible ou un auteur hors registre rendent `("", 0)` : le
    doute ne devine jamais une famille depuis un nom Git seul."""
    if not repo or not sha:
        return "", 0.0
    text = _read_out(repo, "show", "-s", "--format=%ae%x1f%at", sha)
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
    if not actor or not _registered(db, actor):
        return "", 0.0
    return actor, stamp


def commit_ids_written_by(repo: str, commits: Sequence[str], name: str,
                          db=None) -> Optional[list]:
    """Les commits de la plage dont `name` est l'**auteur Git** (`%ae`).

    L'auteur Git est la signature du contenu ; le committer (`commit_actor`)
    est l'agent qui a créé l'objet. Un amend ou un rebase conserve l'auteur
    d'origine et change le committer : les deux ont écrit la plage (#2808).
    Le nom Git seul ne prouve rien : l'adresse `<nom>@ameesh.invalid` est
    exigée, comme pour le committer, ou l'ancienne `<nom>@ameesh.local` datée
    (`%at`) d'avant la convention, vers un agent du registre (#3111).

    Rend la liste des SHA, vide si aucun, `None` si la lecture est impossible
    (un doute n'est pas une absence de preuve). #3742 : le refus de gel nomme
    les commits à renvoyer à l'assigné, d'où la liste et non un booléen."""
    if not repo or not name:
        return []
    shas = [str(sha).strip() for sha in commits or () if str(sha).strip()]
    if not shas:
        return []
    text = _read_out(repo, "log", "--no-walk", "--format=%H%x1f%ae%x1f%at", *shas)
    if text is None or len(str(text).splitlines()) != len(set(shas)):
        return None                     # unreadable is not proof of absence
    wanted = str(name).strip().lower()
    found = []
    for line in str(text).splitlines():
        fields = line.split("\x1f")
        if len(fields) != 3:
            return None
        try:
            stamp = float(fields[2].strip())
        except ValueError:
            return None
        if _agent_of_email(db, fields[1], stamp) == wanted:
            found.append(str(fields[0]).strip())
    return found


def commits_written_by(repo: str, commits: Sequence[str], name: str,
                       db=None) -> Optional[bool]:
    """`True` si un commit de la plage porte `name` comme auteur Git.

    Même lecture que [`commit_ids_written_by`] (#3111) ; le doute reste
    `None`, jamais une absence de preuve."""
    found = commit_ids_written_by(repo, commits, name, db)
    return None if found is None else bool(found)


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
# #3805 : un commit rebasé garde l'auteur de son contenu
# --------------------------------------------------------------------------
#
# Un rebase écrit un nouvel objet : le reçu exact reste sur l'ancien commit et
# le nouveau n'en a pas. Le committer est alors l'agent qui a réécrit, pas
# forcément celui qui a écrit le contenu. Deux preuves se complètent :
#
# * une réémission à patch-id verbatim identique d'un commit qui porte un reçu
#   exact **garde ce reçu** : l'auteur Git (conservé par le rebase) et sa date
#   d'auteur retrouvent l'original, et la copie reste attribuée à son auteur ;
# * sans ce reçu, l'auteur Git d'adresse d'agent **différent du committer** est
#   un auteur du contenu comme le committer (le doute conserve, comme #2924) :
#   les deux familles sont exclues de la relecture.
#
# Un nom Git hors registre ne prouve jamais une famille : le doute tient le
# commit pour inconnu, il ne le crédite pas à un nom deviné.

def _receipt_search_heads(db, item: Mapping, repo: str) -> list:
    """Les gels antérieurs du lot encore lisibles, pour retrouver un reçu."""
    lot = int((item or {}).get("id") or 0)
    if db is None or not lot or not repo:
        return []
    try:
        rows = storage.of(db).work.milestones(lot, None) or []
    except Exception:  # noqa: BLE001 - jalons illisibles : pas de source
        return []
    heads = []
    for row in rows:
        if str(row.get("kind") or "") != "frozen":
            continue
        sha = str(row.get("sha") or "").strip()
        if not sha or sha in heads:
            continue
        try:
            if _read_local_commit(repo, sha):
                heads.append(sha)
        except Exception:  # noqa: BLE001 - dépôt illisible : ce gel est ignoré
            continue
    return heads


def _same_patch_candidates(db, item: Mapping, repo: str, email: str,
                           cache: Optional[dict] = None) -> list:
    """`[(sha, author_stamp)]` des commits non-fusions du même auteur Git.

    `--all` couvre les refs du clone ; les gels antérieurs du lot, souvent
    détachés après un force-push, sont ajoutés comme point de départ quand
    leur objet est encore lisible. Le résultat est mis en cache par appelant :
    une plage rebasée peut contenir plusieurs copies du même auteur.
    """
    if cache is not None and email in cache:
        return cache[email]
    args = ["log", "--all", "--no-merges",
            "--format=%H%x1f%ae%x1f%at%x1f%P", "--author=" + email]
    args.extend(_receipt_search_heads(db, item, repo))
    try:
        log = _read_out(repo, *args)
    except Exception:  # noqa: BLE001 - recherche impossible : pas de reçu
        log = None
    candidates = []
    for line in str(log or "").splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        candidate, candidate_email, raw_stamp, parents = (
            part.strip() for part in parts)
        if candidate_email.lower() != email or len(parents.split()) > 1:
            continue
        try:
            stamp = float(raw_stamp)
        except ValueError:
            continue
        candidates.append((candidate, stamp))
    if cache is not None:
        cache[email] = candidates
    return candidates


def _same_patch_receipts(db, item: Mapping, repo: str, sha: str, *,
                         provider=None, cache: Optional[dict] = None
                         ) -> list:
    """Les reçus exacts des réémissions à patch-id verbatim identique (#3805).

    Le rebase conserve l'auteur Git et sa date ; on cherche, parmi les refs et
    les gels antérieurs du lot, les commits du même auteur au même instant dont
    le patch est identique et qui portent un reçu exact. **Tous** les reçus
    retenus comptent : deux réémissions du même contenu peuvent avoir été
    reçues sous des harnais différents, et en garder un seul ferait disparaître
    une famille selon l'ordre de `git log` (relecture cx1, #3805). L'appelant
    réunit leurs familles ; une ambiguïté se transporte, elle ne se tranche
    pas.

    Une lecture impossible rend une liste vide : c'est une absence de preuve,
    jamais une erreur (le repli committer + auteur Git reste fermé).
    """
    if db is None or not repo or not sha:
        return []
    text = _read_out(repo, "show", "-s", "--format=%ae%x1f%at%x1f%P", sha)
    if text is None:
        return []
    fields = str(text).strip().split("\x1f")
    if len(fields) != 3:
        return []
    email = fields[0].strip().lower()
    try:
        stamp = float(fields[1].strip())
    except ValueError:
        return []
    if len(fields[2].split()) > 1:
        return []                       # un commit de fusion n'a pas ce patch
    actor = _agent_of_email(db, email, stamp)
    if not actor or not _registered(db, actor):
        return []
    candidates = [candidate for candidate, candidate_stamp
                  in _same_patch_candidates(db, item, repo, email, cache)
                  if candidate != sha and candidate_stamp == stamp]
    if not candidates:
        return []
    try:
        wanted = _read_diff_patch_id(repo, sha + "^", sha)
    except Exception:  # noqa: BLE001 - patch illisible : pas de preuve
        return []
    if not wanted:
        return []
    found: list = []
    seen: set = set()
    for candidate in candidates:
        try:
            receipt = _commit_receipt(provider, db, candidate, strict=False)
        except review_provenance.ProvenanceError:
            continue                    # un reçu corrompu ne prouve rien ici
        if not receipt:
            continue
        if str(receipt.get("actor") or "").strip().casefold() != actor.casefold():
            continue
        try:
            patch = _read_diff_patch_id(repo, candidate + "^", candidate)
        except Exception:  # noqa: BLE001 - ce candidat reste sans preuve
            continue
        if not patch or patch != wanted:
            continue
        key = (str(receipt.get("actor") or "").strip().casefold(),
               str(receipt.get("harness") or "").strip().casefold())
        if key in seen:
            continue
        seen.add(key)
        found.append(receipt)
    return found


def _unreceived_role(actor: str, author_name: str, reviewers) -> str:
    """Le rôle d'un acteur dans la lecture : référence, relecteur ou tiers."""
    if author_name and str(actor).casefold() == str(author_name).strip().casefold():
        return "reference"
    if actor in reviewers:
        return "reviewer"
    return "third"


def _unreceived_authors(db, item: Mapping, repo: str, sha: str, *,
                        author_name: str, author_family: str, reviewers,
                        families, provider=None, carry_receipts: bool = True,
                        cache: Optional[dict] = None) -> tuple[list, list]:
    """`(lignes, inconnus)` d'un commit sans reçu exact (#2721/#3805).

    Chaque ligne est `{name, family, role, proof}` ; `role` vaut `reference`,
    `reviewer` ou `third`, et `proof` dit que la famille vient du journal des
    tours (trace de la désignation). Un reçu porté par une réémission
    identique prime sur le committer. Sinon le committer et l'auteur Git
    d'agent, quand ils diffèrent, sont **tous deux** auteurs du contenu : le
    doute conserve les deux familles (#2924). Un tiers sans famille prouvée
    reste inconnu, et un nom Git hors registre n'est jamais lu.

    `carry_receipts=False` (lecture d'un gel antérieur) garde l'attribution
    committer/auteur Git sans relancer la recherche de reçu par patch-id : le
    report des gels antérieurs couvre déjà le contenu qui survit (#2924), et
    une plage historique peut avoir beaucoup de commits sans reçu. `cache`
    partage la liste des candidats d'un auteur entre les copies d'une même
    lecture.
    """
    committer, committer_stamp = commit_actor(repo, sha, db)
    git_author, author_stamp = commit_author(repo, sha, db)
    # #3301 : un objet créé hors d'un tour d'agent (committer hors registre)
    # n'achète pas le reçu d'une copie : le reçu ne vaut que pour une
    # réémission faite par un agent. #3805 : un rebase d'agent en est une.
    agent_made = bool(committer) and (
        (author_name and committer.casefold() == str(author_name).strip().casefold())
        or _registered(db, committer))
    receipts = (_same_patch_receipts(db, item, repo, sha, provider=provider,
                                     cache=cache)
                if agent_made and carry_receipts else [])
    if receipts:
        actor = str(receipts[0].get("actor") or "").strip()
        if not actor:
            return [], [sha[:12]]
        proven = {_family(receipt.get("harness"), families)
                  for receipt in receipts}
        proven.discard("")
        row = {"name": actor,
               "family": next(iter(proven)) if len(proven) == 1 else "",
               "role": _unreceived_role(actor, author_name, reviewers),
               "proof": False}
        if len(proven) > 1:
            # #3584/#3805 : deux réémissions du même contenu reçues sous des
            # harnais différents gardent **toutes** leurs familles ; aucune
            # n'est choisie selon l'ordre de `git log`.
            row["families"] = sorted(proven)
        return [row], []
    if (git_author and committer
            and git_author.casefold() != committer.casefold()
            and agent_made):
        rows, unknown = [], []
        for actor, stamp in ((committer, committer_stamp),
                             (git_author, author_stamp)):
            role = _unreceived_role(actor, author_name, reviewers)
            proven = actor_family(db, actor, stamp, families)
            if role == "reference" and not proven:
                proven = author_family
            if role == "third" and not proven:
                if sha[:12] not in unknown:
                    unknown.append(sha[:12])
                continue
            rows.append({"name": actor, "family": proven, "role": role,
                         "proof": True})
        if not rows:
            return [], [sha[:12]]
        return rows, unknown
    actor, stamp = committer, committer_stamp
    if not actor:
        return [], [sha[:12]]
    role = _unreceived_role(actor, author_name, reviewers)
    if role != "reference" and not _registered(db, actor):
        return [], [sha[:12]]
    if role == "third":
        # #2721 : un tiers non relecteur sans reçu reste inconnu ; lui seul ne
        # prouve pas avoir écrit le contenu de la plage.
        return [], [sha[:12]]
    proven = actor_family(db, actor, stamp, families)
    if role == "reference" and not proven:
        proven = author_family
    return [{"name": actor, "family": proven, "role": role, "proof": True}], []


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
#: #4913 : au-delà de ce nombre de lignes demandées, `git grep -F -f -` coûte
#: plus cher que la lecture complète de l'arbre et de son cache (#4475) ;
#: en dessous, le balayage ciblé ne garde que les lignes demandées, donc une
#: entrée de cache bornée par la demande et jamais par la taille de l'arbre.
#: Mesure release/11ag (arbre de 105 Mio, 22 Mio compressés) : 275 motifs
#: 3,5 s, 409 motifs 16,6 s, 1 115 motifs 40,3 s, arbre complet ~0,8 s plus
#: traitement de la sortie. 384 garde le ciblé sous le point où il devient
#: plus cher que la lecture complète.
TREE_LINES_WHOLE_MAX = 384
#: lignes de code génériques : leur présence ne prouve aucun contenu propre
_TRIVIAL_LINES = frozenset((
    "pass", "return", "return true", "return false", "break", "continue",
    "else:", "try:", "finally:", "except:", "}", ")", "]", "end",
))


def _read_bound(limit=None):
    sweep = active_sweep()
    if sweep is not None:
        sweep.check("git read")
        return sweep.deadline if limit is None else Deadline(min(limit, sweep.deadline.remaining()))
    return Deadline(canon.GIT_TIMEOUT if limit is None else limit)


@contextlib.contextmanager
def _local_object_repo(repo):
    """Read shared objects from a repository with no promisor or remote."""
    objects = _read_out(repo, "rev-parse", "--git-path", "objects")
    if not objects:
        raise plan_git.MergeProbeError("local object directory unreadable")
    objects = os.path.realpath(os.path.join(repo, objects))
    with tempfile.TemporaryDirectory(prefix="ameesh-authors-",
                                     dir=os.environ.get("TMPDIR")) as gitdir:
        proc = plan_git._git(gitdir, "init", "--quiet", "--bare",
                             timeout=plan_git.PROBE_TIMEOUT_S,
                             deadline=_read_bound(plan_git.PROBE_TIMEOUT_S))
        if proc.returncode:
            raise plan_git.MergeProbeError("local reader initialization failed")
        # Preserve the reader's info/attributes semantics (grep_cache keys
        # include them), while leaving all remote/promisor configuration out.
        attributes = _read_out(repo, "rev-parse", "--git-path", "info/attributes")
        if attributes:
            try:
                with open(os.path.join(repo, attributes), "rb") as source:
                    content = source.read()
            except FileNotFoundError:
                pass
            else:
                with open(os.path.join(gitdir, "info", "attributes"), "wb") as target:
                    target.write(content)
        yield gitdir, {**plan_git.NO_LAZY_FETCH, "GIT_DIR": gitdir,
                       "GIT_OBJECT_DIRECTORY": objects}


def _read_git(repo, *args, **kwargs):
    """Content reads use the remaining sweep budget, without lazy fetches."""
    if args[0] == "log" and "--all" in args:
        # The isolated object reader has no refs or HEAD. Resolve --all in
        # the source clone (metadata only), then walk those exact object ids
        # in the remote-free reader. Include detached HEAD, as git --all does.
        refs = _read_out(repo, "for-each-ref", "--format=%(objectname)")
        if refs is None:
            raise plan_git.MergeProbeError("local reference list unreadable")
        head = _read_out(repo, "rev-parse", "--verify", "HEAD")
        revisions = list(dict.fromkeys([*refs.splitlines(), *([head] if head else [])]))
        if revisions:
            args = tuple(arg for arg in args if arg != "--all") + tuple(revisions)
    heavy = args[0] in {"log", "diff", "show", "patch-id"}
    limit = canon.GIT_TIMEOUT if heavy else plan_git.PROBE_TIMEOUT_S
    sweep = active_sweep()
    kwargs["timeout"] = kwargs.get("timeout", (
        sweep.deadline.remaining() if heavy and sweep is not None else limit))
    kwargs["deadline"] = _read_bound(None if heavy else limit)
    try:
        if heavy or args[0] == "merge-tree":
            with _local_object_repo(repo) as (reader, env):
                kwargs["env_extra"] = {**kwargs.get("env_extra", {}), **env}
                return plan_git._git(reader, *args, **kwargs)
        return plan_git._git(repo, *args, **kwargs)
    finally:
        if sweep is not None:
            sweep.check("git " + str(args[0]))


def _read_out(repo, *args):
    try:
        if args[0] in {"log", "show", "diff"}:
            proc = _read_git(repo, *args)
            return proc.stdout.decode("utf-8", "replace").strip() if not proc.returncode else None
        return plan_git._out(repo, *args, deadline=_read_bound(plan_git.PROBE_TIMEOUT_S))
    except (canon.GitError, canon.GitDeadline, plan_git.MergeProbeError):
        return None
    finally:
        sweep = active_sweep()
        if sweep is not None:
            sweep.check("git " + str(args[0]))


def _read_local_commit(repo, rev):
    return plan_git.local_commit(repo, rev, deadline=_read_bound(plan_git.PROBE_TIMEOUT_S))


def _read_patch_ids(repo, *args):
    log = _read_git(repo, "log", "-p", "--no-merges", "--no-color",
                    "--format=commit %H", *args)
    if log.returncode:
        raise plan_git.MergeProbeError("patch history unreadable")
    if not log.stdout.strip():
        return {}
    proc = _read_git(repo, *plan_git.PATCH_ID, stdin=log.stdout)
    if proc.returncode:
        raise plan_git.MergeProbeError("patch ids unreadable")
    return plan_git._parse_patch_ids(proc)


def _read_diff_patch_id(repo, before, after):
    if not _local_reads(repo, [before, after], {}):
        return None
    diff = _read_git(repo, "diff", "--no-ext-diff", "--no-color", before, after)
    if diff.returncode:
        return None
    proc = _read_git(repo, *plan_git.PATCH_ID,
                     stdin=b"commit " + after.encode() + b"\n" + diff.stdout)
    if proc.returncode:
        return None
    values = proc.stdout.decode("utf-8", "replace").split()
    return values[0] if values else None


def _local_reads(repo, revs, cache):
    """Batch presence, including negative/uncertain results, per reading."""
    cache = {} if cache is None else cache
    wanted = list(dict.fromkeys(str(rev) for rev in revs if rev))
    missing = [rev for rev in wanted if ("local-commit", repo, rev) not in cache]
    if missing:
        resolved = {}
        for rev in missing:
            if re.fullmatch(r"[0-9a-fA-F]{7,64}(?:\^[0-9]*)?", rev):
                resolved[rev] = rev.lower()
            else:
                try:
                    resolved[rev] = _read_out(repo, "rev-parse", "--verify", rev)
                except Exception:
                    resolved[rev] = None
        present = _present_commits(repo, [r for r in resolved.values() if r],
                                   timeout=plan_git.PROBE_TIMEOUT_S)
        for rev in missing:
            cache[("local-commit", repo, rev)] = bool(
                present is not None and resolved[rev] in present)
    return all(cache[("local-commit", repo, rev)] for rev in wanted)


def _is_ancestor(repo: str, before: str, after: str, cache=None) -> Optional[bool]:
    """`before` est-il ancêtre de `after` ? None si Git ne peut pas le dire."""
    if not repo or not before or not after:
        return None
    cache = {} if cache is None else cache
    key = ("ancestor", repo, before, after)
    if key not in cache:
        value = None
        if _local_reads(repo, [before, after], cache):
            try:
                proc = _read_git(repo, "merge-base", "--is-ancestor", before, after)
                if proc.returncode in (0, 1):
                    value = proc.returncode == 0
            except Exception:
                pass
        cache[key] = value
    return cache[key]


def _present_commits(repo: str, shas: Sequence[str], *,
                     timeout: Optional[float] = None) -> Optional[dict[str, str]]:
    """Les SHA de commits présents **localement**, en une seule commande git.

    #4457 (gel #25249) : `plan_git.local_commit` lançait un processus git par
    reçu ; 500 reçus prenaient ~40 s, au-delà du budget de 60 s du balayage en
    CI. `cat-file --batch-check` lit la présence des objets en un passage, sans
    fetch paresseux (magasin sans remote), et rend `{sha: sha complet}`.
    `None` quand la lecture échoue : la preuve locale reste non concluante.
    """
    wanted = list(dict.fromkeys(str(sha).lower() for sha in shas or ()
        if re.fullmatch(r"[0-9a-fA-F]{7,64}(?:\^[0-9]*)?", str(sha))))
    if not wanted:
        return {}
    if timeout is not None and timeout <= 0:
        return None
    try:
        with _local_object_repo(repo) as (gitdir, env):
            proc = _read_git(
                gitdir, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                stdin=("\n".join(wanted) + "\n").encode("utf-8"),
                timeout=plan_git.PROBE_TIMEOUT_S if timeout is None else timeout,
                env_extra=env)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    present = {}
    for rev, line in zip(wanted, proc.stdout.decode("utf-8", "replace").splitlines()):
        fields = line.split()
        if len(fields) == 2 and fields[1] == "commit":
            present[rev] = fields[0].lower()
    return present


#: #4673 : borne de la lecture du SHA publié d'une cible avant une lecture de
#: provenance. Une forge muette laisse la lecture locale : le doute garde les
#: auteurs au lieu de rendre une famille indépendante par accident.
TARGET_REFRESH_TIMEOUT = 30.0


def _published_target(repo: str, target: str) -> str:
    """Le commit **publié** d'une cible de branche, lu à la forge (#4673).

    Un clone partagé n'est frais que de son dernier fetch : sa ref
    `refs/remotes/<remote>/<branche>` peut nommer un ancêtre de la cible
    réelle. La porte et la désignation lisaient cette ref locale et comptaient
    donc des commits déjà entrés dans la cible comme des tiers « auteur
    inconnu » (Nexlink #165406, ameesh #4654). Ici la pointe publiée est lue
    par `ls-remote` (lecture seule, aucun ref écrit) puis l'objet exact est
    demandé par SHA à la forge, comme pour un gel absent (#3147) : le clone
    partagé reste intact et un autre agent peut le fetcher en parallèle.

    Rend "" — la lecture locale fait alors foi — pour un commit exact, une
    ref non-branche, un dépôt sans remote, une cible `HEAD`, une forge
    injoignable ou un SHA abrégé. Jamais d'exception : la fraîcheur au mieux,
    le doute fermé ensuite.
    """
    text = str(target or "").strip()
    if not repo or not text:
        return ""
    if plan_git.full_commit_id(text) or re.fullmatch(r"[0-9a-f]{7,39}", text.lower()):
        return ""                          # commit exact : rien à rafraîchir
    if text.startswith("refs/") and not text.startswith(
            ("refs/heads/", "refs/remotes/")):
        return ""                          # étiquette ou ref privée : objet exact
    try:
        remotes = (plan_git._out(repo, "remote") or "").split()
    except Exception:  # noqa: BLE001 - configuration illisible : lecture locale
        return ""
    if not remotes:
        return ""
    try:
        remote = canon.remote_branch(repo, text)
    except Exception:  # noqa: BLE001 - résolution distante impossible : locale
        return ""
    if not remote or "/" not in remote:
        return ""
    name, branch = remote.split("/", 1)
    if name not in remotes or not branch or branch == "HEAD":
        return ""
    try:
        listing = plan_git._git(
            repo, "ls-remote", "--exit-code", "--refs", name,
            "refs/heads/" + branch, timeout=TARGET_REFRESH_TIMEOUT)
    except Exception:  # noqa: BLE001 - forge injoignable : lecture locale
        return ""
    if listing.returncode != 0:
        return ""
    fields = listing.stdout.decode("utf-8", "replace").split()
    sha = plan_git.full_commit_id(fields[0]) if fields else ""
    if not sha:
        return ""
    try:
        if plan_git.local_commit(repo, sha):
            return sha
    except Exception:  # noqa: BLE001 - sonde locale impossible : tenter le fetch
        pass
    try:
        proc = plan_git._git(
            repo, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
            "--", name, sha, timeout=TARGET_REFRESH_TIMEOUT)
    except Exception:  # noqa: BLE001 - forge coupée : lecture locale
        return ""
    if proc.returncode != 0:
        return ""
    try:
        return sha if plan_git.local_commit(repo, sha) else ""
    except Exception:  # noqa: BLE001 - objet non lisible : lecture locale
        return ""


def refreshed_target(repo: str, target: str) -> str:
    """#4673 : le commit publié d'une cible de branche, rafraîchi, ou "".

    Même lecture que `target_commit(fetch=True)` mais **sans repli local** :
    un appelant qui a besoin de distinguer une cible prouvée fraîche d'une
    ref de clone périmée (la base de la porte) ne reçoit que la première.
    """
    return _published_target(repo, target)


def target_commit(repo: str, target: str, *, fetch: bool = False) -> str:
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
    branche ou un tag local homonyme. Une cible `refs/...` se lit exactement.

    #4673 : avec `fetch=True`, une cible de **branche** est d'abord lue à la
    forge par son SHA publié (`_published_target`), puis l'objet exact est
    rapatrié : un clone partagé non rafraîchi ne fait plus passer des commits
    déjà entrés dans la cible pour des tiers. Une forge muette retombe sur la
    ref locale ; `fetch=False` (transaction de gel, #3836) ne touche jamais au
    réseau et lit la ref locale."""
    text = str(target or "").strip()
    if not repo or not text:
        return ""
    if fetch:
        fresh = _published_target(repo, text)
        if fresh:
            return fresh
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
            found = _read_local_commit(repo, ref) or ""
        except Exception:  # noqa: BLE001 - cible illisible : pas d'écart
            found = ""
        if found:
            return found
    return ""


def stacked_parents(db, item: Mapping, repo: str, head: str) -> list[dict]:
    """Les parents d'empilement dont une tête gelée précède `head` (#4478).

    Un lot empilé sur la tête gelée d'un parent — prérequis déclaré
    (`work dependencies`) ou membre du même paquet (#3589) — porte dans sa
    plage, lue depuis la cible, les commits du parent. Ces commits ont été
    relus sur la tête du parent : ils ne sont ni auteurs ni périmètre de
    relecture de l'enfant. La lecture d'auteurs (`reading`, `admitted_heads`)
    part donc de ces têtes.

    Rend `[{lot, frozen, source, target}]`, réduits aux têtes les plus proches
    (`freeze_stack.parent_heads`). On ne rend que ce qui est **prouvé** : un
    parent ouvert, un gel lisible dans ce dépôt et l'ascendance de cette tête
    sur la tête lue. Une lecture impossible (graphe, frise du parent, objet
    git) ne retire rien : le doute conserve les auteurs.

    Comme #3513 pour les têtes des membres admis, une tête absente de
    l'historique de `head` ne retire aucun commit. La porte de fusion ne
    retire en plus que les parents dont la **propre porte** est satisfaite
    (`merge_queue._admitted_stacked_parents`) : un parent jamais relu ou
    bloqué reste dans le périmètre de l'enfant.
    """
    lot = int((item or {}).get("id") or 0)
    if not lot:
        return []
    from . import freeze_stack

    store = storage.of(db).work
    try:
        candidates = freeze_stack.candidates(db, lot)
    except Exception:  # noqa: BLE001 - graphe illisible : aucun parent retiré
        return []
    # #4478 : sans parent candidat, aucune sonde git n'est nécessaire. La
    # résolution locale de `head` ouvre un dépôt d'objets temporaire
    # (`plan_git.local_commit`) : la faire pour tout lot changeait le compte
    # des temporaires d'un post sans radar (#1761, test_l160e) sans rien
    # prouver de plus.
    if not candidates:
        return []
    head_full = _read_local_commit(repo, str(head or ""))
    if not head_full:
        return []
    parents: list[dict] = []
    for candidate in candidates or ():
        parent = int((candidate or {}).get("lot") or 0)
        if not parent:
            continue
        try:
            row = store.get(parent)
            if row is None or str(row.get("state") or "") in freeze_stack.DONE_STATES:
                continue
            rows = store.milestones(parent, None) or ()
        except Exception:  # noqa: BLE001 - parent illisible : aucun retrait
            continue
        for freeze in rows:
            if str((freeze or {}).get("kind") or "") != "frozen":
                continue
            frozen = _read_local_commit(repo, str((freeze or {}).get("sha") or ""))
            if not frozen or frozen == head_full:
                continue
            if canon.git_contains(repo, frozen, head_full):
                parents.append({"lot": parent, "frozen": frozen,
                                "source": str((candidate or {}).get("source") or ""),
                                "target": str(row.get("branch_target") or "")})
    return freeze_stack.parent_heads(repo, parents)


def stacked_parent_heads(db, item: Mapping, repo: str, head: str) -> tuple[str, ...]:
    """Les SHA des têtes gelées des parents d'empilement (#4478)."""
    return tuple(entry["frozen"] for entry in
                 stacked_parents(db, item, repo, head))


def _applied_in(repo: str, target: str, sha: str) -> bool:
    """Le patch est encore appliqué : le rejouer ne change pas la cible.

    Un patch-id historique seul ne suffit pas après un revert ou une
    réécriture. Toute fusion conflictuelle ou lecture impossible garde l'auteur.
    """
    if not _local_reads(repo, [target, sha, sha + "^"], {}):
        return False
    try:
        tree = _tree_of(repo, target)
        if not tree:
            return False
        proc = _read_git(repo, "merge-tree", "--write-tree",
                             "--merge-base", sha + "^", target, sha)
        if proc.returncode != 0:
            return False
        lines = proc.stdout.decode("utf-8", "strict").splitlines()
        return bool(lines and lines[0].strip() == tree)
    except Exception:  # noqa: BLE001 - sans preuve, garder l'auteur
        return False


def _text_delta(repo: str, before: str, after: str, *, path: str = ""):
    """Exact added/removed lines, or None for unreadable/non-text changes.

    Compare globally, including trivial lines and whitespace: a rename or a
    repeated line can only retain an author. Modes, submodules and binaries
    have no line proof and never grant the exception (#3781).
    """
    proc = _read_git(repo, "diff", "--no-color", "--no-ext-diff", "--no-textconv",
                         "--ignore-submodules=none",
                         "--no-renames", "--unified=0", before, after, "--",
                         *([path] if path else []))
    if proc.returncode:
        return None
    text = proc.stdout.decode("utf-8", "strict")
    adds, removes = set(), set()
    hunk = False
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            hunk = False
        elif line.startswith(("Binary files ", "GIT binary patch", "old mode ",
                              "new mode ")):
            return None
        elif line.startswith(("new file mode ", "deleted file mode ")):
            if line.rsplit(" ", 1)[-1] not in ("100644", "100755"):
                return None
        elif line.startswith("index ") and line.endswith(" 160000"):
            return None
        elif line.startswith("@@"):
            hunk = True
        elif hunk and line.startswith("+"):
            adds.add(line[1:])
        elif hunk and line.startswith("-"):
            removes.add(line[1:])
    return adds, removes


def _no_reintroduced_contribution(repo: str, sha: str, landing) -> bool:
    """Le contenu propre du commit est-il réintroduit par la tête (#3902) ?

    Une ligne vide, ou une ligne que le parent du commit portait déjà,
    n'est pas la contribution du commit ; l'inclure retenait indéfiniment des
    copies de l'ancienne pile H7/H8 pourtant livrées dans la cible (#3519).
    Seules les lignes **propres** (ajouts absents du parent) non vides et les
    retraits non vides comptent. Un recoupement fait uniquement de lignes
    héritées ou vides ne réintroduit rien : le commit est déjà livré. Une
    lecture impossible garde le doute.
    """
    if landing is None:
        return False
    try:
        patch = _text_delta(repo, sha + "^", sha)
    except Exception:  # noqa: BLE001 - unreadable candidate retains author
        return False
    if patch is None or not any(patch):
        return False
    added = {row for row in patch[0] if row.strip()}
    removed = {row for row in patch[1] if row.strip()}
    landing_add = {row for row in landing[0] if row.strip()}
    landing_remove = {row for row in landing[1] if row.strip()}
    # Un retrait propre réapparu dans la tête est une réintroduction.
    if removed & landing_remove:
        return False
    overlap = added & landing_add
    if not overlap:
        return True
    # Seul le recoupement avec le landing demandait la preuve d'héritage :
    # une ligne propre hors du landing ne peut pas être réintroduite par lui.
    # Le `git grep` d'arbre entier se limite donc à ces quelques lignes.
    try:
        inherited = _lines_in_tree(repo, sha + "^", sorted(overlap))
    except Exception:  # noqa: BLE001 - doute : garder le commit
        return False
    return all(row.strip() in inherited for row in overlap)


def _patch_in_target(repo: str, target: str, sha: str, cache: dict) -> bool:
    """Le patch de `sha` existe-t-il déjà dans l'histoire de la cible (#3902) ?

    Même preuve bornée que `_not_in_target` (#3762), pour un seul commit :
    patch-id verbatim présent parmi les `MAX_TARGET_COMMITS` derniers commits
    de la cible. Une lecture impossible, un diff vide ou un patch absent
    rendent `False` : le doute garde le commit.
    """
    if not _local_reads(repo, [target, sha, sha + "^"], cache):
        return False
    key = ("patch-in-target", str(target), str(sha))
    if key in cache:
        return bool(cache[key])
    found = False
    try:
        patches = _read_patch_ids(repo, "--no-walk", sha)
        if patches:
            base = _read_out(repo, "merge-base", str(target), sha)
            if base:
                # Une seule lecture de la cible par base : les copies d'un
                # même gel partagent leur point de divergence, et le parcours
                # borné est le poste coûteux de cette preuve.
                index_key = ("target-patch-ids", str(target), str(base))
                if index_key not in cache:
                    try:
                        cache[index_key] = _read_patch_ids(
                            repo, "--max-count=%d" % plan_git.MAX_TARGET_COMMITS,
                            "%s..%s" % (base, str(target)))
                    except Exception as exc:  # unreadable is not "not delivered"
                        cache[index_key] = None
                        sweep = active_sweep()
                        step = "target patch index unreadable: %s" % exc
                        if sweep is not None:
                            sweep.tripped = sweep.tripped or step
                            sweep.trips += 1
                        else:
                            import logging
                            logging.getLogger(__name__).warning(step)
                on_target = cache[index_key]
                found = bool(on_target) and patches.keys() <= on_target.keys()
    except Exception:  # noqa: BLE001 - pas de preuve : garder le commit
        found = False
    cache[key] = found
    return found


def _delivered_paths(repo: str, target: str, tree: str):
    """Exact landing lines by destination path; unreadable changes fail closed."""
    proc = _read_git(repo, "diff", "--no-renames", "--name-only", "-z",
                         target, tree, "--")
    if proc.returncode:
        return None
    result = {}
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        path = raw.decode("utf-8", "strict")
        delta = _text_delta(repo, target, tree, path=path)
        if delta is None:
            return None
        result[path] = delta
    return result


def _outside_delivered(repo: str, target: str, commit: str, landing,
                       cache: dict) -> bool:
    """No candidate line is brought by this landing, including copies/moves.

    Existing occurrences on target never excuse new occurrences in a landing.
    A repeated generic line conservatively retains its historical author.
    Historical removals retain authors even when reverted (#3220); this
    exception must not bypass the existing conservative removal proof.
    """
    if landing is None:
        return False
    try:
        patch = _text_delta(repo, commit + "^", commit)
        if patch is None or not any(patch) or patch[1]:
            return False
        if any(patch[0] & delta[0] or patch[1] & delta[1]
               for delta in landing.values()):
            return False
        cache[("delivered-proof", target, commit)] = {}
        return True
    except Exception:  # noqa: BLE001 - uncertainty retains every author
        return False


#: #4858 : une seule lecture `rev-list --ancestry-path` prouve quelles têtes
#: de gel contiennent le commit inconnu (#4825 : 256 candidats, 2 841 appels
#: git, 149 s). La preuve est locale et bornée ; au-delà (ou si elle échoue)
#: l'inconnu reste inconnu, jamais deviné.
CONTAINMENT_TIMEOUT = 30.0
#: #4858 : au plus ce nombre de gels sources sont relus en profondeur pour un
#: commit inconnu. La mesure de #4825 en lisait 12 pour une attribution et 11
#: par inconnu, après 256 candidats ; le plafond empêche le coût de croître
#: avec les gels de l'application. Au-delà, l'inconnu reste inconnu : une
#: borne ne retire ni n'ajoute jamais un auteur sans preuve.
MAX_ATTRIBUTION_SOURCES = 32


def _containing_heads(repo: str, commit: str, heads: Sequence[str], *,
                      cache: Optional[dict] = None) -> Optional[set]:
    """Les têtes qui contiennent `commit`, en **une** lecture git (#4858).

    `rev-list --ancestry-path ^commit <têtes>` ne rend que les commits sur un
    chemin d'ascendance entre le commit et une tête : l'intersection avec les
    têtes demandées est exactement l'ensemble des têtes dont l'historique
    contient le commit. La forme par candidat lançait un processus git par
    tête (256 par commit inconnu sur #4825) ; celle-ci en lance un seul. Les
    objets absents sont filtrés localement (`_present_commits`, sans fetch
    paresseux) : une tête absente n'est jamais tenue pour contenante. `None`
    quand la preuve est illisible : l'appelant laisse l'inconnu inchangé.
    """
    full = plan_git.full_commit_id(commit)
    if not full and repo and commit:
        try:
            full = _read_local_commit(repo, str(commit)) or ""
        except Exception:  # noqa: BLE001 - forme illisible : pas de preuve
            full = ""
    if not repo or not full:
        return None
    wanted = list(dict.fromkeys(
        value for value in (plan_git.full_commit_id(head) for head in heads or ())
        if value))
    if not wanted:
        return set()
    cache = {} if cache is None else cache
    key = ("containing-heads", repo, full, tuple(wanted))
    if key in cache:
        return cache[key]
    found = None
    try:
        present = _present_commits(repo, [full, *wanted],
                                   timeout=CONTAINMENT_TIMEOUT)
        if present and full in present:
            heads_present = [head for head in wanted if head in present]
            if heads_present:
                proc = plan_git._git(
                    repo, "rev-list", "--ancestry-path", "^" + full,
                    *heads_present, timeout=CONTAINMENT_TIMEOUT,
                    deadline=_read_bound(CONTAINMENT_TIMEOUT),
                    env_extra=canon.NO_LAZY_FETCH)
                if proc.returncode == 0:
                    reached = set(proc.stdout.decode("utf-8", "replace").split())
                    found = {head for head in heads_present if head in reached}
                    # `git rev-list ^C C` élague la tête égale au commit : une
                    # gelée sur ce commit même le contient évidemment.
                    if full in wanted:
                        found.add(full)
            else:
                found = set()
    except Exception:  # noqa: BLE001 - preuve illisible : l'inconnu demeure
        found = None
    cache[key] = found
    return found


def _attributed_from_other_freeze(db, item: Mapping, freeze: Mapping,
                                  commit: str, repo: str, families, *,
                                  provider=None, cache: dict, target: str = ""):
    """Resolve an unknown in its earlier originating lot, never remove it.

    Only exact membership and recorded provenance count. Re-read the exact
    commit using the other lot's historical reference; no recursive carry.
    The freeze heads that contain the commit are proved by one batched
    `rev-list` (#4858), the source item and receipt are read once per source,
    and at most `MAX_ATTRIBUTION_SOURCES` sources are deeply re-read. Missing,
    unreadable or beyond-the-bound candidates leave the unknown unchanged:
    the result never gains an author without recorded proof.
    """
    lot = int(item.get("id") or 0)
    app = str(item.get("app") or "")
    at_ts = freeze.get("at_ts")
    if not app or not lot or at_ts is None:
        return None
    try:
        port = storage.of(db).work
        key = ("other-freezes", app, lot, float(at_ts))
        if key not in cache:
            cache[key] = port.earlier_freezes(app, lot, float(at_ts))
        sources = []
        for source in cache[key] or ():
            source_lot = int(source.get("work_item_id") or 0)
            source_sha = plan_git.full_commit_id(source.get("sha"))
            stamp = source.get("at_ts")
            if (not source_lot or source_lot == lot or not source_sha
                    or stamp is None or float(stamp) > float(at_ts)):
                continue
            sources.append((source, source_lot, source_sha))
        containing = _containing_heads(
            repo, commit, [source_sha for _source, _lot, source_sha in sources],
            cache=cache)
        if containing is None:
            return None
        examined = 0
        for source, source_lot, source_sha in sources:
            if source_sha not in containing:
                continue
            if examined >= MAX_ATTRIBUTION_SOURCES:
                break                     # borne : l'inconnu reste inconnu
            examined += 1
            item_key = ("source-item", source_lot)
            if item_key not in cache:
                cache[item_key] = port.get(source_lot)
            source_item = cache[item_key]
            if (not source_item or source_item.get("app") != app
                    or packet_composer(source_item)):
                continue
            # The source range is above its common baseline with the exact
            # target. A baseline ancestor is never attributed by this proof.
            start_key = ("source-start", str(target), source_sha)
            if start_key not in cache:
                cache[start_key] = (_read_out(repo, "merge-base", target,
                                              source_sha) if target else None)
            start = cache[start_key]
            if not start or _is_ancestor(repo, commit, start, cache) is not False:
                continue
            # Freeze receipt pins the source head.
            receipt_key = ("source-receipt", int(source["id"]))
            if receipt_key not in cache:
                cache[receipt_key] = _milestone_receipt(
                    provider, db, int(source["id"]), strict=True)
            receipt = cache[receipt_key]
            if not receipt or receipt.get("commit") != source_sha:
                continue
            known, written, corrections, unknown = _prior_reading(
                db, source_item, source, [commit], repo, families,
                provider=provider, strict=True)
            if unknown or not known or any(not values or "" in values
                                           for values in known.values()):
                continue
            if any(commit not in written.get(actor, ()) for actor in known):
                continue
            from . import work
            work.note(db, lot,
                      "#3837 : commit %s attribué par le gel #%s du lot #%s ; "
                      "auteurs ajoutés : %s" % (
                          commit, source["id"], source_lot,
                          ", ".join("%s (%s)" % (actor, ", ".join(sorted(values)))
                                    for actor, values in sorted(known.items()))), once=True)
            return known, written, corrections
    except Exception:  # noqa: BLE001 - no recorded proof, no attribution
        return None
    return None


def _git_unquote(raw: str) -> str:
    """Décode un chemin Git entre guillemets C (`"a/\303\251"`), sinon tel quel."""
    value = str(raw or "")
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            data, _ = codecs.escape_decode(value[1:-1].encode("utf-8"))
            return data.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - chemin illisible : tel quel
            return value
    return value


def _diff_path(raw: str) -> Optional[str]:
    """Le chemin d'un en-tête `---`/`+++`, ou None pour `/dev/null`."""
    value = _git_unquote(str(raw or "").strip())
    if value == "/dev/null":
        return None
    if value[:2] in ("a/", "b/"):
        return value[2:]
    return value or None


def _commit_hunks(repo: str, sha: str):
    """`({chemin: [hunk]}, {chemin: (mode ancien, mode nouveau)})` du patch.

    Chaque hunk porte `new` (contexte + lignes ajoutées, image postérieure),
    `old` (contexte + lignes retirées), `changed` (lignes ajoutées/retirées
    seules) et `lead`/`trail` (contexte de tête/queue, identique dans les deux
    images). Les modes viennent des lignes brutes (`--raw`) : un changement
    de mode seul, ou sur un chemin qui change aussi du texte, doit être
    vérifié contre la cible (#3902 B/P2, témoin cx1). Un diff binaire ou une
    lecture impossible rend None : l'appelant garde le doute. Le contexte est
    celui de `--unified=1` : une évolution voisine de la cible empêche la
    preuve au lieu de la contourner. Les renommages sont lus comme
    suppression + création (`--no-renames`).
    """
    text = _raw_out_strict(repo, "show", "--format=", "--raw", "--unified=1",
                           "--no-renames", sha)
    if text is None:
        return None
    hunks: dict = {}
    modes: dict = {}
    path = ""
    old_path = ""
    current = None
    in_hunk = False
    for line in text.split("\n"):
        if line.startswith(":"):
            meta, sep, raw_path = line[1:].partition("\t")
            fields = meta.split()
            if sep and len(fields) >= 2:
                modes[_git_unquote(raw_path)] = (fields[0], fields[1])
            continue
        if line.startswith("diff --git "):
            path, old_path, current, in_hunk = "", "", None, False
            continue
        if line.startswith(("Binary files ", "GIT binary patch")):
            return None
        if not in_hunk and line.startswith("--- "):
            old_path = _diff_path(line[4:]) or ""
            continue
        if not in_hunk and line.startswith("+++ "):
            path = _diff_path(line[4:]) or old_path
            continue
        if line.startswith("@@"):
            if not path:
                return None
            header = re.match(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", line)
            current = {"new": [], "old": [], "changed": [], "lead": 0,
                       "trail": 0, "changed_seen": False,
                       "old_start": int(header.group(1)) if header else 0}
            hunks.setdefault(path, []).append(current)
            in_hunk = True
            continue
        if current is None:
            continue
        if line.startswith("+"):
            current["new"].append(line[1:])
            current["changed"].append(line[1:])
            current["changed_seen"] = True
            current["trail"] = 0
        elif line.startswith("-"):
            current["old"].append(line[1:])
            current["changed"].append(line[1:])
            current["changed_seen"] = True
            current["trail"] = 0
        elif line.startswith(" "):
            current["new"].append(line[1:])
            current["old"].append(line[1:])
            if current["changed_seen"]:
                current["trail"] += 1
            else:
                current["lead"] += 1
    return hunks, modes


def _path_lines(repo: str, rev: str, path: str, cache: dict):
    """`(lignes, saut de ligne final)` de `path` dans `rev`, ou (None, None).

    Le saut de ligne final fait partie du contenu : `git show` le restitue,
    et deux blobs identiques en texte mais pas en fin de fichier ne sont pas
    la même livraison (#3902 B/P2, témoin cx1). Un fichier vide est tenu pour
    terminé par un saut de ligne (la question n'a pas de sens)."""
    key = ("path-lines", str(rev), str(path))
    if key not in cache:
        text = _raw_out_strict(repo, "show", "%s:%s" % (rev, path))
        if text is None:
            cache[key] = (None, None)
        else:
            eof = text.endswith("\n") if text else True
            lines = text.split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            cache[key] = (lines, eof)
    return cache[key]


def _path_mode(repo: str, rev: str, path: str, cache: dict):
    """Mode Git de `path` dans `rev` (`"100644"`, `"100755"`, ...), ou None."""
    key = ("path-mode", str(rev), str(path))
    if key not in cache:
        text = _read_out(repo, "ls-tree", str(rev), "--", str(path))
        if text is None:
            cache[key] = None
        else:
            fields = str(text).split(None, 3)
            cache[key] = fields[0] if len(fields) >= 3 else None
    return cache[key]


def _parsed_hunks(repo: str, sha: str, cache: dict):
    """`_commit_hunks` mis en cache, None si la lecture est impossible."""
    key = ("commit-hunks", str(sha))
    if key not in cache:
        try:
            cache[key] = _commit_hunks(repo, sha)
        except Exception:  # noqa: BLE001 - lecture impossible : doute
            cache[key] = None
    return cache[key]


def _mode_delivery(repo: str, target: str, sha: str, cache: dict) -> Optional[bool]:
    """Les changements de mode de `sha` sont-ils livrés dans `target` ?

    True quand chaque mode postérieur s'y trouve (ou quand la lecture des
    modes est impossible : les autres preuves gardent alors le doute), False
    dès qu'un mode annoncé n'y est pas. Un chemin à mode seul compte comme
    les autres (#3902 B/P2, témoin cx1)."""
    if not _local_reads(repo, [target, sha, sha + "^"], cache):
        return None
    parsed = _parsed_hunks(repo, sha, cache)
    if not parsed:
        return True
    _hunks, modes = parsed
    for path, (_old_mode, new_mode) in sorted(modes.items()):
        # Le mode FINAL compte pour tout chemin touché, même quand le
        # candidat ne change pas le mode : un symlink 120000 remplacé par un
        # fichier ordinaire 100644 au même blob n'est pas le même contenu
        # (#3902 B/P2, témoin cx2).
        if str(new_mode) == "000000":
            if _path_absent(repo, target, path, cache) is not True:
                return False
        elif _path_mode(repo, target, path, cache) != str(new_mode):
            return False
    return True


def _find_block(lines: Sequence[str], block: Sequence[str], start: int) -> int:
    """Index du bloc exact `block` dans `lines` à partir de `start`, sinon -1."""
    if not block:
        return start
    limit = len(lines) - len(block)
    for index in range(max(0, start), limit + 1):
        if list(lines[index:index + len(block)]) == list(block):
            return index
    return -1


def _position_map(parent: Sequence[str], target: Sequence[str]) -> dict:
    """`{index parent: index cible}` des lignes alignées par SequenceMatcher.

    Sert à rattacher le hunk à son occurrence d'origine malgré les
    insertions/suppressions de la cible avant lui. Une ligne non alignée est
    absente de la table : l'appelant garde alors le doute."""
    mapping: dict = {}
    matcher = difflib.SequenceMatcher(None, list(parent), list(target),
                                      autojunk=False)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            mapping[block.a + offset] = block.b + offset
    return mapping


def _anchor_position(hunk: Mapping, mapping: Mapping):
    """`(index antérieur mappé, index postérieur attendu)` du hunk, ou None.

    `mapping` vient de `_position_map(sha^:path, before:path)`. Le bloc est
    rattaché à son occurrence d'origine par son contexte de tête (ou de queue
    s'il commence par un retrait), jamais par une recherche libre : un bloc
    identique dans une autre fonction ne peut donc pas acheter la preuve.
    None quand le hunk n'a pas de contexte alignable (création, ligne isolée
    sans contexte) : l'appelant retombe alors sur la recherche historique."""
    old_start = int(hunk.get("old_start") or 0)
    old = hunk["old"]
    new = hunk["new"]
    lead, trail = hunk["lead"], hunk["trail"]
    if old_start < 1 or not old:
        return None
    p0 = old_start - 1
    if lead:
        mapped = mapping.get(p0)
        if mapped is None:
            return None
        return mapped, mapped
    if trail:
        # le contexte de queue commence après les lignes retirées ; on le
        # mappe, puis on recule du nombre de lignes retirées (ancienne image)
        # ou ajoutées (nouvelle image).
        anchor = p0 + len(old) - trail
        mapped_trail = mapping.get(anchor)
        if mapped_trail is None:
            return None
        mapped_old = mapped_trail - (len(old) - trail)
        expected = mapped_trail - (len(new) - trail)
        if expected < 0:
            return None
        return mapped_old, expected
    return None


def _anchor_holds(parent: Sequence[str], target: Sequence[str], hunk: Mapping,
                  mapped_old: int, expected: int, window: int = 3) -> bool:
    """Le contexte immédiat autour du hunk confirme-t-il le point d'ancrage ?

    Vérifie jusqu'à `window` lignes avant l'ancien bloc et après le nouveau
    bloc. Une zone voisine modifiée dans la cible fait échouer l'ancrage :
    c'est un doute, jamais une preuve par un bloc identique ailleurs. Un
    `mapped_old` négatif est légitime quand le hunk commence avant la
    première ligne de la cible (suppression en tête de fichier)."""
    old_start = int(hunk["old_start"]) - 1
    old_len, new_len = len(hunk["old"]), len(hunk["new"])
    before = list(parent[max(0, old_start - window):old_start])
    if mapped_old >= 0:
        if list(target[max(0, mapped_old - window):mapped_old]) != before:
            return False
    elif before:
        # le début du hunk est avant la première ligne de la cible : aucun
        # contexte antérieur ne peut exister.
        return False
    after = list(parent[old_start + old_len:old_start + old_len + window])
    start = expected + new_len
    return list(target[start:start + len(after)]) == after


def _content_before_head(repo: str, before: str, sha: str,
                         cache: Optional[dict] = None) -> bool:
    """Preuve positive que le patch de `sha` est déjà dans `before` (#3902).

    Le résultat est mis en cache par `(cible, sha)` : la désignation et la
    porte relisent le même commit, et l'alignement coûte cher sur un gros
    fichier (#3902 N de dev1)."""
    if not _local_reads(repo, [before, sha, sha + "^"], cache):
        return False
    cache = {} if cache is None else cache
    key = ("content-before", str(before), str(sha))
    if key not in cache:
        cache[key] = _content_before_head_value(repo, before, sha, cache)
    return bool(cache[key])


def _content_before_head_value(repo: str, before: str, sha: str,
                               cache: Optional[dict] = None) -> bool:
    """Preuve positive que le patch de `sha` est déjà dans `before` (#3902).

    Un gel antérieur peut porter les commits d'un lot parent que la cible a
    depuis intégrés **sous d'autres SHA** (squash, empilement, réécriture) :
    ces commits ne comptent ni comme auteurs ni comme inconnus du lot courant.
    `before` est la cible courante, lue avant la fusion de la tête : aucune
    ligne apportée par la tête elle-même ne peut acheter cette preuve.

    La preuve est volontairement stricte et **positive** :

    * diff lisible et non binaire, avec au moins une ligne significative ;
    * par chemin, chaque hunk est prouvé **entier** : son image postérieure
      (contexte `--unified=1` + lignes ajoutées), ordre et indentation
      compris, apparaît telle quelle **à l'occurrence d'origine** du hunk
      (celle de `sha^:path`, mappée dans `before` par alignement) ; les hunks
      se suivent dans l'ordre du commit ;
    * un point d'ancrage ambigu (contexte répété, ligne non alignée) ou un
      contexte voisin modifié dans la cible est un doute : un bloc identique
      dans une autre fonction ne peut pas acheter la preuve (#3902 B/P2,
      témoins cx2) ;
    * au même endroit, l'image antérieure du hunk (contexte + lignes
      retirées) ne doit pas être encore là : une suppression restée en place
      n'est pas une livraison, même quand le contexte seul y ressemble ;
    * les modes postérieurs du commit, y compris un chemin à mode seul dans
      un commit qui change du texte ailleurs, sont vérifiés contre la cible :
      un mode encore absent est un doute (#3902 B/P2, témoin cx1) ;
    * une suppression totale de chemin se prouve par l'absence du chemin ;
    * un réordre ou une réindentation change l'image : il n'est pas prouvé
      par le seul compte de lignes.

    Toute lecture impossible, tout hunk non prouvé, tout déplacement de
    chemin est un doute : le commit reste (le doute ne retire jamais un
    auteur). Un commit sans ligne significative et sans changement de mode
    (binaire, vide, blanc) n'est jamais prouvé : il reste aussi.
    """
    cache = {} if cache is None else cache
    parsed = _parsed_hunks(repo, sha, cache)
    if not parsed:
        return False
    hunks, modes = parsed
    if not hunks and not modes:
        return False
    meaningful = False
    for path, (old_mode, new_mode) in sorted(modes.items()):
        # Le mode final est vérifié pour tout chemin touché, même sans
        # changement de mode dans le candidat (#3902 B/P2, témoin cx2) ;
        # seul un vrai changement de mode rend le commit significatif.
        if str(new_mode) == "000000":
            if _path_absent(repo, before, path, cache) is not True:
                return False
            if str(old_mode) != str(new_mode):
                meaningful = True
            continue
        if _path_mode(repo, before, path, cache) != str(new_mode):
            return False
        if str(old_mode) != str(new_mode):
            meaningful = True
    for path, path_hunks in sorted(hunks.items()):
        here, here_eof = _path_lines(repo, before, path, cache)
        here_present = here is not None
        if not here_present:
            if _path_absent(repo, before, path, cache) is not True:
                return False
            here = []
            here_eof = True
        after, after_eof = _path_lines(repo, sha, path, cache)
        if after is None:
            if _path_absent(repo, sha, path, cache) is not True:
                return False
        elif here_present and bool(here_eof) != bool(after_eof):
            # Le saut de ligne final fait partie du contenu : la cible peut
            # porter le même texte sans la même fin de fichier (#3902 B/P2,
            # témoin cx1). Le doute garde l'auteur.
            return False
        parent, _parent_eof = _path_lines(repo, str(sha) + "^", path, cache)
        if parent is None:
            if _path_absent(repo, str(sha) + "^", path, cache) is not True:
                return False
            parent = []
        mapping = _position_map(parent, here)
        position = 0
        for hunk in path_hunks:
            changed = [row for row in hunk["changed"] if row.strip()]
            if any(_meaningful_line(row) for row in changed):
                meaningful = True
            block = hunk["new"]
            if not block:
                if _path_absent(repo, before, path, cache) is not True:
                    return False
                continue
            old = hunk["old"]
            end = len(old) - hunk["trail"] if hunk["trail"] else len(old)
            old_core = old[hunk["lead"]:end]
            if hunk["lead"] or hunk["trail"]:
                # Ancrage sur l'occurrence d'origine : le bloc postérieur doit
                # se trouver exactement au point mappé, et le contexte voisin
                # doit le confirmer (un ancrage ambigu est un doute).
                anchor = _anchor_position(hunk, mapping)
                if anchor is None:
                    return False
                mapped_old, index = anchor
                if index < position:
                    return False
                if not _anchor_holds(parent, here, hunk, mapped_old, index):
                    return False
                if list(here[index:index + len(block)]) != block:
                    return False
            else:
                # ni contexte de tête ni de queue : avec une image antérieure
                # non vide, le hunk couvre tout le fichier (réécriture
                # entière) ; le bloc doit être le fichier entier, jamais un
                # bloc trouvé ailleurs (#3902 B2 dev1). Pour une création
                # (ancienne image vide), la recherche historique reste la
                # seule preuve possible, et le doute la borne.
                if old:
                    # L'image postérieure commence au début du fichier (le
                    # hunk n'a pas de contexte de tête) et l'ancien contenu
                    # ne doit plus y figurer, même après une évolution. Un
                    # changement de seule fin de fichier a old == new : c'est
                    # la comparaison des sauts de ligne qui tranche.
                    if list(here[:len(block)]) != list(block):
                        return False
                    if (list(old) != list(block)
                            and _find_block(here, old, 0) >= 0):
                        return False
                    index = 0
                else:
                    index = _find_block(here, block, position)
                    if index < 0:
                        return False
            # L'image antérieure du hunk ne doit pas être encore à sa place :
            # une suppression non appliquée n'est pas une livraison, même si
            # son contexte seul se retrouve tel quel (suppression en bout de
            # fichier, par exemple). Les lignes de contexte de tête sont
            # communes aux deux images ; on compare donc le cœur du hunk.
            if old_core and list(old_core) != list(block):
                if len(block) == hunk["lead"] + hunk["trail"]:
                    # suppression pure : le bloc postérieur n'est que du
                    # contexte, il peut se retrouver décalé. Dans l'image
                    # antérieure le cœur retiré est juste après le contexte de
                    # tête, ou juste avant le contexte de queue s'il n'y en a
                    # pas.
                    if hunk["lead"]:
                        start = index + hunk["lead"]
                        if list(here[start:start + len(old_core)]) == old_core:
                            return False
                    elif hunk["trail"]:
                        start = index - len(old_core)
                        if (start >= 0
                                and list(here[start:index]) == old_core):
                            return False
                else:
                    start = index + hunk["lead"]
                    if list(here[start:start + len(old_core)]) == old_core:
                        return False
            position = index + len(block)
    return meaningful


def _not_in_target(repo: str, commits, target: str, *, head: str = "",
                   cache: Optional[dict] = None) -> list:
    """Les commits qui ne sont dans `target` ni par ascendance ni par patch.

    #3116 : un commit déjà dans la cible a été relu sous son propre lot ; il
    ne compte ni comme auteur ni comme tiers du lot, qu'il vienne de la plage
    courante ou du report d'un gel antérieur (#2924). La lecture se fait en un
    seul `git rev-list ... --not <cible>`, sans une sonde par commit ; une
    lecture impossible garde la liste entière (le doute ne retire jamais un
    auteur, il ne le compte pas non plus dans la cible).

    #3323 : la sortie de `rev-list` est canonique (complète, minuscules) tandis
    qu'un appelant peut passer une entrée abrégée ou en majuscules. Comparer
    l'entrée telle quelle l'écartait alors comme « dans la cible », à l'envers
    du doute : on ne retire désormais que l'entrée **prouvée** dans la cible,
    et une entrée non résolue reste. Un identifiant complet est normalisé sans
    sonde ; seule une forme non canonique est résolue localement.

    #3762 : un rebase change le SHA sans changer le patch. Les copies déjà
    intégrées dans la cible ne portent plus d'auteur du lot, y compris dans
    les gels antérieurs. Les patch-id **verbatim** conservent les espaces
    (significatifs en Python) ; fusions, diffs vides ou lecture impossible
    restent dans la liste. La recherche cible est bornée ; dépasser la borne
    ne peut que conserver des auteurs, jamais en retirer sans preuve. Un
    patch historique n'est écarté que si son rejeu laisse la cible inchangée,
    ou si la fusion propre de `head` prouve qu'aucune de ses lignes ajoutées
    ou retirées n'est réintroduite (#3781). #3902 : une preuve de contenu sur
    la cible courante couvre aussi les patchs intégrés sous un autre SHA ;
    sans tête, elle ne s'applique pas. Le doute conserve l'auteur après
    revert/réécriture."""
    cache = {} if cache is None else cache
    wanted = [str(commit).strip() for commit in commits or ()
              if str(commit).strip()]
    if not wanted or not target:
        return wanted
    _local_reads(repo, [*wanted, *(sha + "^" for sha in wanted), target, head], cache)
    if not _local_reads(repo, [target], cache):
        return wanted
    absent = [sha for sha in wanted if not _local_reads(repo, [sha], cache)]
    wanted = [sha for sha in wanted if sha not in absent]
    if absent:
        return absent + _not_in_target(repo, wanted, target, head=head, cache=cache)
    try:
        text = _read_out(repo, "rev-list", *wanted, "--not", str(target))
    except Exception:  # noqa: BLE001 - lecture impossible : aucun écart
        return wanted
    if text is None:
        return wanted
    outside = {line.strip() for line in str(text).splitlines() if line.strip()}
    kept = []
    for commit in wanted:
        canonical = plan_git.full_commit_id(commit)
        if not canonical:
            canonical = _read_local_commit(repo, commit) or ""
        # `outside` est la sortie de rev-list : les commits NON atteignables
        # depuis la cible. Seule une entrée absente de cet ensemble est prouvée
        # dans la cible ; non résolue, elle reste (le doute ne retire jamais).
        if canonical and canonical not in outside:
            continue
        kept.append(canonical or commit)
    if not kept:
        return kept
    try:
        # Borne commune à TOUS les candidats : exclure l'union de leurs
        # ancêtres masquerait la cible dès qu'un candidat descend de celle-ci.
        base = _read_out(repo, "merge-base", "--octopus", str(target), *kept)
        if not base:
            return kept
        # Un seul parcours de la cible, puis lecture des seuls candidats.
        on_target = _read_patch_ids(
            repo, "--max-count=%d" % plan_git.MAX_TARGET_COMMITS,
            "%s..%s" % (base, target))
        if not on_target:
            return kept
    except Exception:  # noqa: BLE001 - pas de preuve : garder les auteurs
        return kept
    equivalent = set()
    # #3902 : la preuve de contenu se lit sur la cible **courante**. Un commit
    # d'un gel antérieur dont le patch y est déjà (squash, empilement,
    # réécriture) ne compte ni comme auteur ni comme inconnu : son contenu est
    # livré, la tête n'en est pas l'auteur. Le doute (lecture impossible,
    # contenu absent) garde le commit.
    before = str(target) if head else ""
    for sha in set(kept):
        if not _local_reads(repo, [sha + "^"], cache):
            continue
        try:
            # Une entrée par commit : deux copies du même patch doivent être
            # écartées toutes les deux (le dictionnaire est indexé par patch).
            patches = _read_patch_ids(repo, "--no-walk", sha)
        except Exception:  # noqa: BLE001 - ce candidat reste dans le doute
            continue
        # Un mode encore absent de la cible est un doute : aucune autre
        # preuve (contenu, patch-id, rejeu) ne peut écarter le commit tant
        # que sa livraison complète n'est pas établie (#3902, témoin cx1).
        if _mode_delivery(repo, str(target), sha, cache) is False:
            continue
        if before and _content_before_head(repo, before, sha, cache):
            equivalent.add(sha)
            continue
        subset = bool(patches) and patches.keys() <= on_target.keys()
        if not subset:
            # Patch intégré sous un autre SHA, mais son contenu n'est plus
            # dans la cible : rien ne prouve qu'il y est livré.
            continue
        if _applied_in(repo, str(target), sha):
            equivalent.add(sha)
            continue
        # #3781 : le patch est historique et son rejeu échoue (cible évoluée
        # ou revert). La contribution propre réintroduite par la tête garde
        # l'auteur ; une ligne vide ou héritée ne le retient pas (#3902).
        if not _no_reintroduced_contribution(
                repo, sha, _landing_delta(repo, str(target), head, cache)):
            continue
        equivalent.add(sha)
    return [sha for sha in kept if sha not in equivalent]


def _survives_rewrite(repo: str, head: str, sha: str, cache: dict,
                      base: str = "", target: str = "") -> bool:
    key = ("survives-rewrite", repo, head, sha, base, target)
    if key not in cache:
        try:
            cache[key] = _survives_rewrite_value(repo, head, sha, cache, base, target)
        except Exception:
            cache[key] = True  # unreadable evidence never removes an author
    return cache[key]


def _survives_rewrite_value(repo: str, head: str, sha: str, cache: dict,
                      base: str = "", target: str = "") -> bool:
    """Un commit d'un gel antérieur compte-t-il encore pour la tête lue ?

    #3116 : un commit déjà dans la cible ne compte ni comme auteur ni comme
    tiers. Hors cible, un commit qui n'est plus ancêtre de la tête **et** dont
    le contenu ne survit pas (preuve de contenu #2924/#2977) a été réellement
    réécrit : il ne bloque pas la lecture. Un commit encore ancêtre de la tête
    reste lu comme avant ; une lecture impossible est un doute, qui garde.

    #3902 : hors cible par SHA, un commit d'un gel antérieur dont le contenu
    # est présent dans la cible avant la tête ne compte pas : il est livré,
    # et la tête n'en est pas l'auteur (une copie réintroduite en plus ne
    # change pas ce fait). Si le contenu n'y est plus mais que le patch-id
    # appartient à l'histoire de la cible (squash, empilement), le rejeu
    # prouve qu'il est encore appliqué ; sinon le garde-fou de réintroduction
    # ne retient que sa contribution **propre**, car une ligne vide ou héritée
    # retenait indéfiniment les copies H7/H8. Sans preuve, le doute garde.
    """
    if not _local_reads(repo, [head, sha, base, target], cache):
        return True
    if not head:
        return True
    if target and _is_ancestor(repo, sha, target, cache) is True:
        return False
    if target and _mode_delivery(repo, str(target), sha, cache) is not False:
        if _content_before_head(repo, str(target), sha, cache):
            return False
        if _patch_in_target(repo, str(target), sha, cache):
            if _applied_in(repo, str(target), sha):
                return False
            if _no_reintroduced_contribution(
                    repo, sha,
                    _landing_delta(repo, str(target), head, cache)):
                return False
    if _is_ancestor(repo, sha, head, cache) is True:
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
    return _read_out(repo, "rev-parse", str(rev) + "^{tree}")


def _merged_tree(repo: str, left: str, right: str) -> Optional[str]:
    """L'arbre de la fusion `left`+`right`, ou None (conflit ou illisible)."""
    if not repo or not left or not right:
        return None
    proc = _read_git(repo, "merge-tree", "--write-tree", left, right)
    if proc.returncode != 0:
        return None
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    return lines[0].strip() if lines else None


def _landing_delta(repo: str, target: str, head: str, cache: dict):
    """Le delta livré par `head` sur `target`, mis en cache, ou None (doute).

    La fusion peut être lue plusieurs fois pour une même lecture (un candidat
    par gel antérieur) : l'arbre fusionné et le diff sont identiques, et la
    cible est épinglée pour toute la lecture. Une lecture impossible rend None,
    que les appelants traitent comme un doute (jamais comme un landing vide).
    """
    key = ("landing", str(target), str(head))
    if key not in cache:
        value = None
        try:
            tree = _merged_tree(repo, str(target), head)
            if tree:
                value = _text_delta(repo, str(target), tree)
        except Exception:  # noqa: BLE001 - doute : garder les auteurs
            value = None
        cache[key] = value
    return cache[key]


def _commits_between(repo: str, start: str, head: str) -> Optional[list]:
    """Les commits de contenu de `start..head`, ou None si illisible."""
    if not repo or not start or not head:
        return None
    text = _read_out(repo, "rev-list", "--no-merges", "%s..%s" % (start, head))
    if text is None:
        return None
    return [line.strip() for line in str(text).splitlines() if line.strip()]


def _commit_lines(repo: str, sha: str):
    """`({chemin: [lignes ajoutées]}, {chemin: [lignes retirées]}, binaire)`.

    Le diff est lu hunk par hunk : un en-tête `+++ b/` ou `--- a/` n'est jamais
    confondu avec une ligne de contenu qui commencerait par `++`/`--`. Un diff
    binaire ne se compare pas ligne à ligne : le lecteur conserve l'auteur
    (jamais l'inverse)."""
    text = _read_out(repo, "show", "--format=", "--unified=0",
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
        text = _read_out(repo, "show", "%s:%s" % (head, path))
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
        text = _read_out(repo, "ls-tree", "--name-only", head, "--", path)
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
        text = _read_out(repo, "diff", "--name-status", "-z", "-M",
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
            proc = _read_git(repo, "ls-tree", "-r", "-z", rev)
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
        blob = _read_out(repo, "rev-parse", "%s:%s" % (sha, path))
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
        blob = _read_out(repo, "rev-parse", "%s:%s" % (sha, path))
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
        text = _read_out(repo, "show", "%s:%s" % (head, path))
        cache[key] = (None if text is None
                      else [line.strip() for line in str(text).splitlines()])
    return cache[key]


def _changed_paths(repo: str, head: str, sha: str, cache: dict):
    """Les chemins A/M/R/C de `sha..head`, None si la lecture échoue.

    #3220/mesh-design : la recherche de bloc reste bornée à ces chemins (et au
    chemin d'origine). `D` est exclu : il ne porte plus de contenu."""
    key = ("changed-paths", sha, head)
    if key not in cache:
        text = _read_out(repo, "diff", "--name-status", "-z", "-M",
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
        parent = _read_out(repo, "rev-parse", "--verify", "--quiet",
                               str(sha) + "^")
        if parent is None:
            cache[key] = None
        else:
            text = _read_out(
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
            def scan():
                # #3974 : seule la liste brute de `grep -l` est gardée par
                # l'hôte (clé : arbre, motifs) ; le filtre de ligne entière
                # ci-dessous relit les fichiers (cache #3753).
                try:
                    proc = _sweep_grep(repo, rev,
                                       ("--no-color", "-l", "-I", "-F"), wanted,
                                       "git grep -l -I -F -f - %s" % rev)
                except SweepDeadline:
                    raise
                except Exception:  # noqa: BLE001 - lecture impossible : doute
                    return None
                if proc is None or proc.returncode not in (0, 1):
                    return None
                if proc.returncode == 1:
                    return []
                prefix = str(rev) + ":"
                return [line[len(prefix):] if line.startswith(prefix) else line
                        for line in proc.stdout.decode(
                            "utf-8", "replace").splitlines() if line]
            listed = grep_cache.cached(repo, str(rev), "sources-l-I", wanted,
                                       scan, wait=_cache_wait())
            if listed is None:
                cache[key] = None
            else:
                sources = set()
                for path in listed:
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
        parent = _read_out(repo, "rev-parse", "--verify", "--quiet",
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
        text = _read_out(repo, "show", "%s:%s" % (rev, path))
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
    proc = _read_git(repo, *args)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def _raw_out_strict(repo: str, *args: str) -> Optional[str]:
    """Comme `_raw_out`, mais refuse un contenu non-UTF-8 (None = doute).

    `_content_before_head` compare des blobs et des hunks : un décodage
    `replace` y confondrait des octets distincts (`\x80` et `\x81` donnent
    le même U+FFFD) et pourrait écarter un auteur dont la contribution reste
    à livrer (#3902 B/P2, témoin cx2). Le doute conserve l'auteur."""
    proc = _read_git(repo, *args)
    if proc.returncode != 0:
        return None
    try:
        return proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


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

    Jusqu'à `TREE_LINES_WHOLE_MAX` lignes demandées, le balayage est ciblé
    (`git grep -F -f -`) : le cache ne garde que les lignes trouvées, donc une
    entrée bornée par la demande, jamais par la taille de l'arbre (#4913).
    Au-delà, l'arbre est lu en entier avec un seul motif universel, puis mis
    en cache indépendamment des lignes demandées (#4475). L'intersection en
    Python évite le coût motifs × arbre de `git grep -F`. Le format de sortie
    est fixé : ni couleur, ni numéro, ni colonne (#2994 B1 cx1).
    `binary=True` lit aussi les fichiers binaires comme du texte (`-a`) ;
    sinon ils sont ignorés (`-I`)."""
    wanted = {str(line).strip() for line in lines or () if str(line).strip()}
    if not wanted:
        return set()
    if not repo or not rev:
        return None
    flag = "-a" if binary else "-I"
    targeted = (len(wanted) <= TREE_LINES_WHOLE_MAX
                and all("\n" not in line and "\x00" not in line
                        and "\ufffd" not in line
                        for line in wanted))
    sweep = active_sweep()
    if sweep is not None:
        sweep.check("git grep -h %s %s -f - %s" %
                    (flag, "-F" if targeted else "-G", rev))
    patterns = sorted(wanted)

    def matches():
        try:
            proc = _sweep_grep(repo, rev, ("--no-color", "--no-line-number",
                                           "--no-column", "-h", flag, "-F"),
                               patterns,
                               "git grep -h %s -F -f - %s" % (flag, rev))
        except SweepDeadline:
            raise
        except Exception:  # noqa: BLE001 - lecture impossible : doute
            return None
        if proc.returncode == 1:
            return []
        if proc.returncode != 0:
            return None
        # Ne garder que les lignes demandées : un motif court peut correspondre
        # à des milliers de lignes, et l'entrée doit rester bornée par la
        # demande (#4913).
        return sorted({line.strip() for line in
                       proc.stdout.decode("utf-8", "replace").splitlines()
                       if line.strip()} & wanted)

    def scan():
        try:
            proc = _sweep_grep(repo, rev, ("--no-color", "--no-line-number",
                                           "--no-column", "-h", flag, "-G"),
                               ["^"], "git grep -h %s -G -f - %s" % (flag, rev))
        except SweepDeadline:
            raise
        except Exception:  # noqa: BLE001 - lecture impossible : doute
            return None
        if proc.returncode == 1:
            return []
        if proc.returncode != 0:
            return None
        return sorted({line.strip() for line in
                       proc.stdout.decode("utf-8", "replace").splitlines()
                       if line.strip()})

    # Un motif multiligne, à NUL ou portant U+FFFD (une ligne décodée depuis
    # des octets invalides) ne peut pas passer par `-f -` : le motif part en
    # UTF-8 sur stdin et ne correspondrait jamais aux octets bruts de l'arbre,
    # alors que la lecture complète, décodée en `replace`, le retrouve
    # (#4964 ; sans la garde, `_any_line_in_head` écartait l'auteur).
    if targeted:
        # Le mode et les motifs entrent dans la clé : deux demandes
        # différentes ne se remplacent plus l'une l'autre (#4913).
        found = grep_cache.cached(repo, str(rev), "tree-match" + flag,
                                  patterns, matches, wait=_cache_wait())
        return None if found is None else wanted.intersection(found)

    # Le mode distingue ce résultat des entrées ciblées et des anciennes
    # entrées limitées aux motifs. grep_cache lie aussi la clé à l'arbre
    # immuable et à ses attributs (#3974).
    found = grep_cache.cached(repo, str(rev), "tree-lines" + flag, [], scan,
                              wait=_cache_wait())
    return None if found is None else wanted.intersection(found)


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
            cache[key] = _read_out(repo, "merge-base", head, base) or ""
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
        cache[key] = _read_out(repo, "merge-base", head, base) or ""
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
    if not _local_reads(repo, [head, sha, sha + "^", base], cache):
        return True
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


def _surviving_review_commits(repo: str, head: str, base: str,
                              review_commits: Mapping,
                              cache: dict) -> dict:
    """Les commits de relecteur dont le contenu survit encore dans la tête (#3335).

    La trace « correcteur » et la promotion en auteur (#2888) suivent la même
    preuve de contenu que #3220 : un commit de relecteur dont aucune ligne
    significative ne subsiste dans la tête gelée n'est plus une correction de
    cette tête, et sa famille redevient éligible. Une ligne qui survit suffit
    (`_commit_survives`) ; une lecture impossible conserve le commit, donc
    l'exclusion (le doute ne retire jamais une famille). Sans `head` (ou sans
    dépôt), l'appelant n'a pas fourni de tête à prouver : rien n'est filtré.

    Un correcteur peut avoir plusieurs commits : chacun est jugé séparément,
    et l'acteur ne reste trace que si au moins un survit.
    """
    if not repo or not head:
        return dict(review_commits or {})
    out: dict = {}
    for actor, entries in (review_commits or {}).items():
        kept = []
        for entry in entries or ():
            sha = str(entry[0] if isinstance(entry, (tuple, list)) else entry)
            try:
                survives = _commit_survives(repo, head, sha, cache, base)
            except Exception:  # noqa: BLE001 - lecture impossible : doute
                survives = True
            if survives:
                kept.append(entry)
        if kept:
            out[actor] = kept
    return out


def _surviving_author_commits(repo: str, head: str, base: str,
                              written: Mapping, cache: dict) -> dict:
    """Les commits d'auteur tiers dont le contenu survit encore dans la tête (#4179).

    Un contenu hérité d'un parent empilé puis retiré de la tête (revert, second
    commit qui annule le premier) n'est pas un contenu de cette tête : son
    auteur n'a pas à exclure sa famille. C'est la même preuve de contenu que
    #3335/#3220, étendue au tiers non relecteur : une ligne significative qui
    survit suffit à garder l'auteur, une lecture impossible le garde aussi (le
    doute ne retire jamais une famille). La référence du lot et les correcteurs
    gardent leur propre lecture (`reading` / `_surviving_review_commits`), les
    auteurs des gels antérieurs leur preuve dédiée (`_carried_from_previous`).

    `written` associe un acteur à ses SHA de la plage courante ; rend le même
    mapping restreint aux commits qui survivent. Sans `head` (ou sans dépôt),
    l'appelant n'a pas fourni de tête à prouver : rien n'est filtré.
    """
    if not repo or not head:
        return dict(written or {})
    out: dict = {}
    for actor, shas in (written or {}).items():
        kept = []
        for sha in shas or ():
            try:
                survives = _commit_survives(repo, head, str(sha), cache, base)
            except Exception:  # noqa: BLE001 - lecture impossible : doute
                survives = True
            if survives:
                kept.append(sha)
        if kept:
            out[actor] = kept
    return out


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
        try:
            # #4804: fire may pass origin/main. The isolated object reader
            # has no refs; resolve source metadata before merge-tree there.
            if not plan_git.full_commit_id(base):
                base = _read_local_commit(repo, base) or base
            merged = _merged_tree(repo, freeze_sha, base)
        except (canon.GitError, canon.GitDeadline, plan_git.MergeProbeError):
            return True
        finally:
            sweep = active_sweep()
            if sweep is not None:
                sweep.check("merge-tree preservation")
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
                   strict: bool = False, ignore_missing=None, target: str = "",
                   head: str = "", base: str = "", cache: Optional[dict] = None,
                   ignore_unknown: bool = False
                   ) -> tuple[dict, dict, dict, list]:
    """`(auteurs, commits_écrits, corrections, inconnus)` d'un gel antérieur.

    Même attribution que `reading` — reçu exact, puis committer et auteur Git
    d'adresse d'agent avec le journal des tours (#2721/#3805) — mais sans la
    reprise des gels encore plus anciens. `commits_écrits` associe à chaque
    acteur les commits qu'il a écrits : c'est la preuve de contenu de #2924.
    `corrections` porte les commits des relecteurs désignés ; leur
    qualification de « correctif de premier tour » se fait dans
    `_prior_correctors` (#2888).

    `ignore_missing(sha)` (#3301) ne dispense que les commits sans reçu et
    sans attribution : jamais un auteur prouvé ou un reçu contradictoire.
    #3116 : `target` est la cible au moment de la lecture. Ses commits ne
    comptent ni comme auteurs ni comme tiers. Hors cible, un inconnu qui n'est
    plus ancêtre de `head` et dont le contenu ne survit pas ne bloque plus
    (`_survives_rewrite`) ; `cache` partage la preuve de contenu.

    `ignore_unknown` (#3399, avis auditeur) : un gel **déclaré remplacé** et
    hors de toute ref garde ses auteurs **connus** dont le contenu survit ; seuls
    ses inconnus (commit sans reçu ni attribution) cessent de bloquer. Un reçu
    contradictoire (`record_unreadable`) reste bloquant : ce n'est pas une
    famille inconnue mais une provenance invalide."""
    reference = _freeze_reference(db, item, freeze, families)
    author_name = str(reference.get("name") or "").strip()
    author_family = str(reference.get("family") or "")
    reviewers = designated_reviewers(db, item, freeze, author_name)
    by_name: dict[str, set] = {}
    written: dict[str, list] = {}
    review_commits: dict[str, list] = {}
    # #3805 : familles prouvées par une correction de relecteur dont le reçu
    # vient d'une réémission ambiguë, par commit ; la promotion en auteur les
    # réunit pour les seuls commits retenus.
    review_families: dict[str, dict[str, set]] = {}
    unknown: list[str] = []
    cache = {} if cache is None else cache
    commits = _not_in_target(repo, commits, target, head=head, cache=cache)

    def record_unknown(value: str) -> None:
        """Un inconnu ne bloque que s'il compte encore pour la tête (#3116),
        et qu'il n'est pas dispensé comme obsolete (#3301). #3399 : un inconnu
        d'un gel remplacé et pendant ne bloque plus (ses auteurs connus sont
        lus par la même passe)."""
        if ignore_unknown:
            return
        if not _survives_rewrite(repo, head, value, cache, base, target):
            return
        if ignore_missing is not None and ignore_missing(value):
            return
        unknown.append(str(value)[:12])

    def record_unreadable(value: str) -> None:
        """Un reçu contradictoire ou vide n'est jamais dispensé par #3301 ;
        #3116 écarte seulement ce qui ne compte plus pour la tête."""
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
            record_unreadable(sha)
            continue
        if receipt is None:
            if not _local_reads(repo, [sha], cache):
                record_unknown(sha)
                continue
            rows, missing = _unreceived_authors(
                db, item, repo, sha, author_name=author_name,
                author_family=author_family, reviewers=reviewers,
                families=families, provider=provider, carry_receipts=False)
            for value in missing:
                record_unknown(value)
            for row in rows:
                actor = row["name"]
                written.setdefault(actor, []).append(sha)
                values = {str(value) for value in row.get("families") or ()
                          if str(value or "").strip()}
                if row["family"]:
                    values.add(str(row["family"]))
                if row["role"] == "reviewer":
                    review_commits.setdefault(actor, []).append(
                        (sha, row["family"]))
                    review_families.setdefault(actor, {}).setdefault(
                        sha, set()).update(values)
                else:
                    by_name.setdefault(actor, set()).update(values)
            continue
        actor = str(receipt.get("actor") or "").strip()
        if not actor:
            record_unreadable(sha)
            continue
        proven = _family(receipt.get("harness"), families)
        written.setdefault(actor, []).append(sha)
        if actor != author_name and actor in reviewers:
            review_commits.setdefault(actor, []).append((sha, proven))
        else:
            by_name.setdefault(actor, set()).add(proven)
    # #2924 B4 : la lecture courante promeut tout commit de relecteur en auteur
    # (#2888). La lecture d'un gel antérieur doit faire de même, même quand le
    # commit n'a pas de reçu exact et que la famille vient du journal des tours
    # (#2721) : sinon un squash à contenu identique perdrait cette famille.
    for actor, entries in review_commits.items():
        if _test_only_correction(repo, entries, str(item.get("app") or "")):
            continue
        by_name.setdefault(actor, set())
        for sha, proven in entries:
            if proven:
                by_name[actor].add(proven)
            # #3805 : une correction reçue sous plusieurs harnais garde toutes
            # ses familles, pas la première de `git log`.
            by_name[actor].update(
                review_families.get(actor, {}).get(sha) or ())
    return by_name, written, review_commits, unknown


def _authored_patch(db, repo: str, sha: str, cache: dict):
    """`(auteur Git d'agent, patch-id verbatim)`, ou None si non prouvé.

    #3301 : ce n'est pas un reçu du commit. La paire ne sert qu'à reconnaître
    une copie sans reçu réémise **par son auteur** avec un reçu exact. Le
    patch-id verbatim conserve les espaces ; un diff vide ou illisible ne
    peut donc pas acheter cette équivalence."""
    if sha not in cache:
        cache[sha] = None
        if not _local_reads(repo, [sha, sha + "^"], cache):
            return None
        try:
            text = _read_out(repo, "show", "-s", "--format=%ae%x1f%at", sha)
            fields = str(text or "").split("\x1f")
            if len(fields) != 2:
                return None
            author = _agent_of_email(db, fields[0], float(fields[1]))
            if not author:
                return None
            patch = _read_diff_patch_id(repo, sha + "^", sha)
            if patch:
                cache[sha] = (author.casefold(), patch)
        except Exception:  # noqa: BLE001 - comparaison impossible : inconnu
            pass
    return cache[sha]


def _received_patch_authors(db, repo: str, commits: Sequence[str], families,
                            cache: dict, *, provider=None) -> set:
    """Les patches réémis par leur auteur avec reçu exact dans la plage lue."""
    out = set()
    for sha in commits:
        try:
            receipt = _commit_receipt(provider, db, sha, strict=True)
        except review_provenance.ProvenanceError:
            continue                    # une preuve invalide n'excuse rien
        if not receipt or not _family(receipt.get("harness"), families):
            continue
        pair = _authored_patch(db, repo, sha, cache)
        if pair and pair[0] == str(receipt["actor"]).strip().casefold():
            out.add(pair)
    return out


def _received_patch_ids(db, repo: str, commits: Sequence[str],
                        cache: dict, *, actor: str, provider=None) -> set:
    """Les patch-id réémis verbatim par `actor` avec un reçu exact (#4387).

    Même preuve que [`_received_patch_authors`] : le reçu nomme l'acteur et
    l'auteur Git du commit est cet acteur — un reçu de committer ne prouve pas
    l'auteur Git. Sert à l'exception du canon §2 (tête d'un gel antérieur
    réécrite par l'auteur de la lecture, commit retiré et cité) : le patch de
    l'ancienne tête réémis par la référence avec son propre reçu n'est plus un
    inconnu. Un `actor` vide ne prouve rien : aucun acteur anonyme
    n'officialise un patch.
    """
    wanted = str(actor or "").strip().casefold()
    out: set = set()
    if not wanted or db is None or not repo:
        return out
    for sha in commits or ():
        try:
            receipt = _commit_receipt(provider, db, sha, strict=True)
        except review_provenance.ProvenanceError:
            continue                    # une preuve invalide n'excuse rien
        if not receipt:
            continue
        if str(receipt.get("actor") or "").strip().casefold() != wanted:
            continue
        pair = _authored_patch(db, repo, sha, cache)
        if pair and pair[0] == wanted:
            out.add(pair[1])
    return out


def _prior_correctors(repo: str, review_commits: Mapping, project: str = "") -> dict:
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
        test_only = _test_only_correction(repo, entries, project)
        if not test_only and (design or total > REVIEWER_FIX_LINES):
            continue
        proven = {value for _sha, value in entries if value}
        family = next(iter(proven)) if len(proven) == 1 else ""
        # #2924 B6 : l'ensemble des familles prouvées est transporté, pas
        # seulement sa réduction à une famille unique (vide si ambiguë).
        out[actor] = {"name": actor, "family": family,
                      "families": sorted(proven)}
        if test_only:
            out[actor]["test_only"] = True
    return out


def _superseded_freeze_ids(freezes: Sequence[Mapping]) -> set[int]:
    """Les gels qu'un gel postérieur déclare remplacés (#2989).

    La marque est écrite par `ameesh work milestone frozen --supersedes <id>`
    (jamais recopiée d'une note d'appelant) : elle vaut pour un gel dont le SHA
    n'existe nulle part (le cas d'un gel fantôme écrit avant la validation de
    la CLI) ou dont l'objet, laissé pendant par un force-push, n'est plus
    contenu par une ref (#3399). Elle ne dispense jamais de lire un gel encore
    **accessible par une ref** : le refus fermé de #2924 B5 reste la règle
    quand le contenu est encore atteignable par l'histoire. Pour un gel
    remplacé hors de toute ref, la lecture garde ses auteurs connus dont le
    contenu survit et ne laisse plus bloquer ses seuls inconnus (#3399, avis
    auditeur)."""
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


class PriorFreezeUnknown(str):
    """Un inconnu de provenance porté par un gel antérieur (#3835).

    Sous-classe de `str` : les appelants qui comparent, joignent ou tronquent
    un SHA continuent de fonctionner. Les attributs disent d'où vient le
    doute, pour que le refus nomme le gel, son SHA et le lot au lieu de rendre
    un SHA nu que l'orchestrateur ne peut rattacher à rien."""

    freeze_id: int = 0
    freeze_sha: str = ""
    lot: int = 0
    reachable: bool | None = None
    reason: str = ""
    repo: str = ""

    def __new__(cls, value, *, freeze_id: int = 0, freeze_sha: str = "",
                lot: int = 0, reachable: bool | None = None,
                reason: str = "", repo: str = ""):
        obj = super().__new__(cls, str(value))
        obj.freeze_id = int(freeze_id or 0)
        obj.freeze_sha = str(freeze_sha or "")
        obj.lot = int(lot or 0)
        obj.reachable = reachable
        obj.reason = str(reason or "")
        obj.repo = str(repo or "")
        return obj


def _supersedes_remedy(lot: int, head_sha: str, freeze_ids: Sequence[int]) -> str:
    """La commande exacte qui déclare ces gels remplacés (#3835)."""
    flags = " ".join("--supersedes %d" % int(value) for value in freeze_ids)
    return ("`ameesh work milestone %d frozen --sha %s %s`"
            % (int(lot), str(head_sha or "").strip() or "<current head>", flags))


def _access_detail(reason: str, repo: str) -> str:
    """Dit un motif de doute d'accessibilité sans le confondre avec un refus."""
    if reason == "timeout":
        return ("accessibility not established: the ref reachability check "
                "(`name-rev`/`for-each-ref --contains`) timed out after %gs in %s"
                % (plan_git.REACHABLE_TIMEOUT_S, repo or "the repository"))
    if reason:
        return ("accessibility not established: %s in %s"
                % (reason, repo or "the repository"))
    return ""


def prior_freeze_problem(lot: int, freeze_id: int, sha: str, detail: str, *,
                         head_sha: str = "", reachable: bool | None = None,
                         reason: str = "", repo: str = "") -> str:
    """Le refus d'un gel antérieur illisible, nommé et actionnable (#3835)."""
    text = "prior freeze #%d" % int(freeze_id)
    if sha:
        text += " (%s)" % str(sha)[:12]
    text += " of lot #%d: %s" % (int(lot), detail)
    said = _access_detail(reason, repo)
    if said:
        text += "; " + said
    if reachable is False and freeze_id:
        text += ("; no local ref contains this freeze: if no remote ref contains "
                 "it either, declare it replaced with %s"
                 % _supersedes_remedy(lot, head_sha, [freeze_id]))
    elif reachable is True:
        text += ("; a ref still contains this freeze, so `--supersedes` would be "
                 "refused: its authors must stay carried")
    elif sha:
        text += ("; its accessibility is not proven, so `--supersedes` cannot be "
                 "offered yet")
    return text


def unknown_refusal(unknown: Sequence[str], *, head_sha: str = "") -> str:
    """Le refus actionnable d'inconnus de provenance (#3835).

    Chaque inconnu porté par un gel antérieur est nommé avec son gel, son SHA
    et le lot. Un délai de `ref_reachable` est dit comme tel. Le remède
    `--supersedes` n'est proposé que pour les gels dont l'absence de toute ref
    **locale** est prouvée ; la commande vérifie ensuite les refs distantes et
    refuse si l'une les contient. Un gel encore accessible ou d'accessibilité
    inconnue est signalé, jamais présenté comme remplaçable. Les inconnus de la
    plage courante, sans gel antérieur, gardent le message historique."""
    names: list[str] = []
    unreachable: dict[int, list[int]] = {}
    reachable: list[PriorFreezeUnknown] = []
    doubts: list[PriorFreezeUnknown] = []
    for value in unknown or ():
        if not isinstance(value, PriorFreezeUnknown):
            names.append(str(value))
            continue
        text = str(value)
        text += (" (prior freeze #%d sha %s of lot #%d"
                 % (value.freeze_id, value.freeze_sha[:12], value.lot))
        said = _access_detail(value.reason, value.repo)
        if said:
            text += "; " + said
        text += ")"
        names.append(text)
        if value.reachable is False and value.freeze_id:
            unreachable.setdefault(value.lot, []).append(value.freeze_id)
        elif value.reachable is True:
            reachable.append(value)
        else:
            doubts.append(value)
    out = "author provenance unknown for frozen commit(s): " + ", ".join(names or ["?"])
    for frozen_lot, freeze_ids in sorted(unreachable.items()):
        unique = sorted({int(value) for value in freeze_ids})
        out += ("; no local ref contains prior freeze(s) %s of lot #%d: if no "
                "remote ref contains them either, declare them replaced with %s"
                % (", ".join("#%d" % value for value in unique), frozen_lot,
                   _supersedes_remedy(frozen_lot, head_sha, unique)))
    for value in reachable:
        out += ("; prior freeze #%d (%s) is still reachable from a ref, so "
                "`--supersedes` would be refused: its authors must stay carried"
                % (value.freeze_id, value.freeze_sha[:12]))
    for value in doubts:
        out += ("; the accessibility of prior freeze #%d (%s) is not proven, "
                "so `--supersedes` cannot be offered yet; retry once the "
                "repository answers" % (value.freeze_id, value.freeze_sha[:12]))
    return out


def _forge_fetch(repo: str, sha: str, *, timeout: Optional[float] = None):
    """Rapatrie le commit exact `sha` depuis la forge du clone (`origin`).

    #3147 : la forge sert encore par SHA un commit devenu inaccessible après un
    push forcé ; un clone récent ne l'a pas dans ses objets. La lecture de
    provenance ne doit donc pas dépendre du clone : elle demande le seul commit
    exact à `origin`, sans déplacer aucune ref et sans écrire `FETCH_HEAD`.
    Un SHA abrégé ne se demande pas (Git exige l'identifiant complet), un
    dépôt sans `origin` n'a pas de forge, et un objet que la forge ne sert pas
    laisse l'absence : le refus fermé de #2924 B5 reste la règle.

    #3648 : le dépôt scratch de `merge-pr` (`git init`, sans `origin`) porte
    sa forge dans `ameesh.forge` (`plan_git.forge_remote`) ; sans elle, un
    gel antérieur écrasé par push forcé n'y était jamais rapatrié.

    #4457 (B/P2 cx3) : `timeout` est le temps qui reste à l'appelant ; le
    fetch ne peut pas dépasser son échéance ni `FETCH_TIMEOUT`. Un temps nul
    ne lance aucun fetch.

    Rend `True` (objet lisible), `False` (refus **explicite de l'objet** :
    seul cas mémorisable par `fetch_cache`) ou `None` (non lancé, coupé par
    l'échéance, panne DNS/connexion/authentification/serveur : un doute qui
    se relit). #4457 B/P2 auditeur : une coupure notée « refusée » pour 6 h
    effacerait le correcteur de toutes les lectures suivantes ; B/P2 cx2 : un
    code 128 de transport n'est pas un refus de l'objet, et le cache ne doit
    pas court-circuiter `recover` après rétablissement de la forge.
    """
    text = plan_git.full_commit_id(sha)
    if not text:
        return None
    try:
        if plan_git.local_commit(repo, text):
            return True
    except Exception:  # noqa: BLE001 - dépôt illisible : aucune récupération
        return None
    bound = plan_git.FETCH_TIMEOUT if timeout is None else min(
        float(timeout), plan_git.FETCH_TIMEOUT)
    sweep = active_sweep()
    if sweep is not None:
        sweep.check("recover prior freeze")
        bound = min(bound, sweep.deadline.remaining())
    if bound <= 0:
        return None
    try:
        forge = plan_git.forge_remote(repo)
        if forge is None:
            return None
        proc = plan_git._git(
            repo, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
            "--", forge, text, timeout=bound)
    except Exception:  # noqa: BLE001 - forge injoignable ou coupée : doute
        return None
    if proc.returncode != 0:
        return False if _fetch_refused(proc.stderr) else None
    try:
        return bool(plan_git.local_commit(repo, text))
    except Exception:  # noqa: BLE001 - lecture impossible : doute
        return None


#: stderr d'un `git fetch` qui refuse **explicitement** l'objet demandé (#4457)
_FETCH_REFUSAL_MARKERS = (
    "not our ref",
    "couldn't find remote ref",
    "remote ref does not exist",
    "no such ref",
    "unadvertised object",
    "not allowed to request",
)


def _fetch_refused(stderr) -> bool:
    """Le `git fetch` a-t-il explicitement refusé **cet objet** ? (#4457)

    Un code non nul ne suffit pas : DNS, connexion, authentification et 5xx
    rendent aussi 128. Seuls les messages qui nomment l'objet absent/non servi
    autorisent le cache négatif ; une panne reste un doute qui se relit (B/P2
    cx2 sur la tête 8298ee2e9).
    """
    text = (stderr.decode("utf-8", "replace")
            if isinstance(stderr, (bytes, bytearray)) else str(stderr or ""))
    low = text.lower()
    return any(marker in low for marker in _FETCH_REFUSAL_MARKERS)


def prefetch_prior_freezes(db, item: Mapping, repo: str, head: str = "") -> list[str]:
    """Rapatrie, **hors transaction**, les objets des gels antérieurs (#3836).

    La désignation d'un gel lit les gels antérieurs du lot pour savoir quels
    auteurs leur contenu porte encore. Quand un objet n'est plus dans le clone
    (rebase, push forcé), `reading` le demandait à la forge par SHA exact —
    `git fetch`, jusqu'à `FETCH_TIMEOUT` (180 s) par objet. Appelé depuis la
    transaction du gel, ce fetch laissait la session `idle in transaction`
    pendant sept minutes, tenant le verrou du dépôt de migration et le verrou
    consultatif du projet (incident du 27/10, #3836).

    Cette fonction fait le même rapatriement **avant** l'ouverture de la
    transaction, sur le dépôt que la désignation lira. Elle est de mieux : le
    gel ne peut plus bloquer le mesh sur le réseau. Un objet que la forge ne
    sert pas reste absent ; la lecture du gel le traite comme avant (un gel
    remplacé est ignoré, un autre rend la désignation en attente).

    Rend les SHA effectivement rapatriés. Jamais d'exception : une forge
    injoignable est une absence, pas une erreur du gel.
    """
    try:
        lot = int((item or {}).get("id") or 0)
    except (TypeError, ValueError):
        return []
    if not lot or not repo:
        return []
    try:
        rows = storage.of(db).work.milestones(lot, None) or []
    except Exception:  # noqa: BLE001 - jalons illisibles : rien à rapatrier
        return []
    current = str(head or "").strip().lower()
    fetched: list[str] = []
    for row in rows:
        if row.get("kind") != "frozen":
            continue
        sha = str(row.get("sha") or "").strip().lower()
        if not sha or sha == current or not plan_git.full_commit_id(sha):
            continue
        try:
            if plan_git.local_commit(repo, sha):
                continue
        except Exception:  # noqa: BLE001 - dépôt illisible : rien à faire
            continue
        try:
            if _forge_fetch(repo, sha):
                fetched.append(sha)
        except Exception:  # noqa: BLE001 - forge injoignable : absence
            continue
    return fetched


def _carried_from_previous(db, item: Mapping, milestone: Optional[Mapping],
                           commits: Sequence[str], repo: str, families, *,
                           base: str = "", head: str = "", provider=None,
                           strict: bool = False, target: str = "",
                           recover=None, admitted_heads=(),
                           exclusions_out: Optional[list] = None,
                           commits_out: Optional[dict] = None,
                           fetch: bool = True,
                           reference_name: str = "") -> tuple[list, list, list]:
    """Auteurs/correcteurs des gels antérieurs dont le contenu subsiste (#2924).

    Rend `(auteurs, inconnus, correcteurs)` à fusionner dans la lecture de la
    tête. Un lot porteur de paquet, une lecture sans base de données ou sans
    dépôt, ou un lot sans gel antérieur rendent des listes vides. Toute
    provenance illisible d'un gel antérieur qui n'est pas déjà couverte par la
    plage courante ajoute un inconnu : l'appelant strict refuse, la désignation
    attend (jamais de famille supposée).

    `commits_out` (#3742) reçoit, pour chaque acteur conservé, les SHA écrits
    dans les gels antérieurs ; il ne sert qu'à nommer les commits d'un refus de
    gel et ne change aucune décision.

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

    #3399 : un force-push laisse l'ancien gel présent dans l'ODB mais hors de
    toute ref. Un gel déclaré remplacé est alors lu **pour ses auteurs connus**
    (le contenu qui survit les porte, #2924), mais ses inconnus ne bloquent plus
    la désignation (avis auditeur) ; un gel resté accessible par une ref garde
    la lecture normale et ses inconnus (B5). Le marqueur n'est écrit qu'après
    la preuve d'inaccessibilité locale **et distante** (#2989/#3399).

    #3301 : un commit antérieur sans reçu et sans attribution ne reste inconnu
    que si son contenu subsiste sans équivalent reçu. Une réémission par le
    même auteur Git d'agent doit porter un reçu exact de cet auteur et le même
    patch-id verbatim dans la plage courante. Cela n'atteste jamais le vieux
    committer et ne retire aucun auteur déjà prouvé ; une contradiction reste
    refusée. Un contenu prouvé disparu n'ajoute plus d'inconnu non plus.

    #4387 : `reference_name` est l'auteur de référence de la tête lue. La
    **tête** d'un gel antérieur sans reçu, **écrite par l'acteur du gel** et
    sortie de l'histoire, réémise à patch-id verbatim identique **par cette
    référence** avec son reçu exact dans la plage courante, ne reste pas
    inconnue : c'est le geste du canon §2 (le correctif d'un relecteur est
    réécrit par l'auteur, le commit retiré et cité). Le §2 borne le geste au
    **petit** correctif sans conception (`REVIEWER_FIX_LINES`) et exclut le
    correcteur **par son nom**, famille éligible. Un commit tiers de la plage
    antérieure garde son inconnu (#2721), et la même réémission par un autre
    acteur ne vaut rien (#3301).
    #3116 : `target` est la cible au moment de la lecture (un commit ou une
    ref). Un commit qui y est déjà entré a été relu sous son propre lot : il ne
    compte ni comme auteur ni comme tiers, même s'il apparaît dans la plage
    d'un gel antérieur calculée avant un rebase. Un gel entièrement contenu
    dans la cible n'est pas relu. Sans `target` lisible, `base` (toujours un
    ancêtre de la cible) sert de borne sûre.

    `fetch=False` (#3836) interdit toute récupération : ni `recover`, ni la
    forge du clone. C'est la lecture d'une transaction de gel — un `git fetch`
    de 180 s y laissait la session `idle in transaction` et retenait ses
    verrous. Les objets manquants sont alors une absence prouvée localement,
    et l'appelant les rapatrie avant la transaction s'il les veut
    (`prefetch_prior_freezes`)."""
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
    if current is None and head_sha and not head_id:
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
    target_sha = target_commit(repo, target, fetch=fetch) or target_commit(
        repo, base, fetch=fetch)
    if head_id:
        # #3948 (N dev4) : l'id de tête absent des gels est le **nouveau** gel
        # — `prepare` le prépare avant l'INSERT, au-delà des marques du lot.
        # Tous les gels existants lui sont antérieurs, y compris quand on
        # re-gèle un sha déjà gelé : la lecture ne se tronque plus à l'ancien
        # gel de ce sha (son contenu, et celui des gels arrivés depuis, reste
        # reportable).
        prior = [row for row in freezes
                 if int(row.get("id") or 0) < head_id]
    elif current is not None:
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
    # #4387 (avis auditeur B1) : les acteurs des têtes de gel dispensées
    # restent exclus nominalement du choix des relecteurs (§2) ; leur famille
    # n'est pas exclue (le correctif est repris par l'auteur).
    dispensed: set[str] = set()
    unknown: list[str] = []
    processed: list[str] = []
    survival: dict[str, bool] = {}
    file_cache: dict = {}
    _local_reads(repo, [head_sha, base, target_sha,
                       *(str(row.get("sha") or "") for row in prior)], file_cache)
    patch_cache: dict = {}
    received_patches = None
    reference_patches = None
    delivered, delivered_read = None, False

    def reemitted_by_reference(patch: str) -> bool:
        """#4387 : ce patch a-t-il été réémis par l'auteur de référence ?

        Seul le geste du canon §2 (correctif de relecteur réécrit par
        l'auteur) dispense un inconnu ; `_received_patch_ids` exige le reçu
        exact de la référence et son auteur Git. Un patch absent ou un acteur
        vide ne dispense rien.
        """
        nonlocal reference_patches
        if not reference_name or not patch:
            return False
        if reference_patches is None:
            reference_patches = _received_patch_ids(
                db, repo, commits, patch_cache,
                actor=reference_name, provider=provider)
        return patch in reference_patches

    def obsolete_missing(commit: str, *, stored: bool, freeze_head: str,
                         freeze_actor: str) -> bool:
        nonlocal received_patches
        if plan_git.full_commit_id(commit) in current_set:
            return False                # aucune dispense de la plage courante
        if not stored:
            if commit not in survival:
                survival[commit] = _commit_survives(
                    repo, head_sha, commit, file_cache, base)
            if not survival[commit]:
                return True             # ce contenu n'est plus dans la tête
        pair = _authored_patch(db, repo, commit, patch_cache)
        if pair is None:
            return False
        if received_patches is None:
            received_patches = _received_patch_authors(
                db, repo, commits, families, patch_cache, provider=provider)
        if pair in received_patches:
            return True
        # #4387 : la réémission par la référence ne vaut que pour la **tête**
        # d'un gel antérieur, écrite par l'acteur de ce gel, sortie de
        # l'histoire. Un commit tiers de la plage antérieure que l'auteur
        # recopierait garde son inconnu (contenu tiers qui survit, #2721/
        # #3116), et un ancêtre de la tête garde sa lecture d'ascendance.
        if not plan_git.same_commit(commit, freeze_head):
            return False
        if str(freeze_actor or "").strip().casefold() != pair[0]:
            return False
        if _is_ancestor(repo, commit, head_sha) is not False:
            return False
        # Le §2 ne permet à l'auteur de reprendre qu'un **petit** correctif
        # (même seuil que `_prior_correctors`) : une tête de gel plus grosse
        # ou de conception garde son inconnu (avis auditeur B2).
        measured = change(repo, commit)
        if measured is None or measured[1] or measured[0] > REVIEWER_FIX_LINES:
            return False
        if not reemitted_by_reference(pair[1]):
            return False
        dispensed.add(str(freeze_actor).strip())
        return True

    def survives(written: Mapping, stored: bool, actor: str, *,
                 every: bool = False, content: bool = False) -> bool:
        """Le contenu écrit par `actor` dans ce gel subsiste-t-il dans la tête ?

        #3220 : un commit déjà présent dans la plage courante est attribué par
        la lecture de cette tête ; le re-prouver par le contenu serait
        redondant, et un gros commit gelé coûterait un `git grep` complet sur
        tout l'arbre. Seuls les commits du gel antérieur que la plage courante
        ne couvre pas demandent la preuve de contenu.

        #3335 : `content=True` (la trace « correcteur ») ne se contente plus de
        l'ascendance (`stored`) : un commit postérieur de la tête peut avoir
        retiré le correctif. La preuve de contenu de #3220 s'applique alors aux
        commits que la plage courante ne couvre pas ; ceux qu'elle couvre sont
        filtrés par `_surviving_review_commits`. Une lecture impossible rend
        `True` (le doute garde l'exclusion).

        `every=True` exige que toutes les corrections de l'acteur survivent ;
        `every=False` (défaut) en exige une. #3338 : la trace « correcteur »
        d'un gel antérieur suit la règle de `_surviving_review_commits`, une
        correction qui survit suffit."""
        shas = written.get(actor) or []
        if not shas:
            return False
        if stored and not content:
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
            return _local_reads(repo, [str(commit or "").strip()], file_cache)
        except Exception:  # noqa: BLE001 - dépôt illisible : absence
            return False

    access_cache: dict[str, bool | None] = {}
    access_why: dict[str, str] = {}

    def at_ref(commit: str) -> bool | None:
        """Le commit est-il contenu par une **ref** du dépôt de lecture (#3399) ?

        `False` : objet pendant (force-push) ou absent — la présence dans l'ODB
        n'est pas consultée, c'est justement ce que #3399 distingue. `None` :
        accessibilité illisible (doute) ; l'appelant relit alors le gel, le
        refus fermé B5 garde sa force pour un contenu peut-être accessible.

        #3835 : le motif d'un `None` (délai ou lecture git en échec) est gardé
        pour que le refus le dise, et le résultat est mis en cache : un gel
        antérieur peut produire plusieurs inconnus, et le relevé est le geste
        lent de la lecture (`for-each-ref --contains`, ~8 s sur un gros clone).
        """
        key = str(commit or "").strip()
        if key in access_cache:
            return access_cache[key]
        reasons: list[str] = []
        try:
            full = key if present(key) else ""
            if not full:
                access_cache[key] = False
                return False
            access_cache[key] = plan_git.ref_reachable(repo, full, why=reasons)
        except Exception:  # noqa: BLE001 - lecture impossible : doute
            access_cache[key] = None
        if reasons:
            access_why[key] = reasons[0]
        return access_cache[key]

    def recover_one(commit: str) -> bool:
        """Tente de rapatrier un gel absent ; True seulement s'il est lisible.

        #3147 : la forge du clone est **toujours** tentée par SHA exact, même
        sans `recover` d'appelant ; `recover` reste une source supplémentaire
        (dépôt de l'agent, autre origine) essayée d'abord quand elle est
        fournie. Un hook qui échoue ne prive pas la forge de sa tentative ;
        une forge qui ne sert pas l'objet laisse le refus fermé.

        #3836 : `fetch=False` coupe les deux sources. La lecture du gel tourne
        dans la transaction qui tient les verrous du lot et du dépôt de
        migration ; un fetch de forge y a laissé une session `idle in
        transaction` sept minutes (incident du 27/10). Les objets doivent être
        rapatriés **avant** la transaction (`prefetch_prior_freezes`).
        """
        try:
            if present(commit):
                return True
        except Exception:  # noqa: BLE001 - dépôt illisible : absence
            return False
        if not fetch:
            return False
        if recover is not None:
            try:
                if recover(commit):
                    file_cache.pop(("local-commit", repo, commit), None)
                    if present(commit):
                        return True
            except Exception:  # noqa: BLE001 - source en échec : la forge reste
                pass
        try:
            if _forge_fetch(repo, commit):
                file_cache.pop(("local-commit", repo, commit), None)
                return present(commit)
            return False
        except Exception:  # noqa: BLE001 - récupération en échec : absence
            return False

    for row in sorted(prior, key=lambda value: (value.get("at_ts") or 0,
                                                int(value.get("id") or 0)),
                      reverse=True):
        row_id = int(row.get("id") or 0)
        ignore_unknown = False
        if row_id in superseded:
            # #2989 : un gel déclaré remplacé dont le SHA n'existe nulle part est
            # un gel fantôme : son contenu n'a jamais existé, et le refus fermé
            # bloquerait la provenance pour toujours. #3399 : un force-push
            # laisse l'ancien gel présent dans l'ODB mais hors de toute ref.
            # La note du gel remplaçant porte la trace (marque machine +
            # événement de gel).
            prior_sha = str(row.get("sha") or "").strip()
            if not prior_sha:
                continue
            access = at_ref(prior_sha)
            if access is False:
                # #3399 (avis auditeur) : un gel remplacé hors de toute ref
                # garde ses auteurs **connus** dont le contenu survit ; seuls
                # ses inconnus cessent de bloquer. Un fantôme absent et
                # irrécupérable n'a aucun contenu à porter : ignoré (#2989).
                if not present(prior_sha) and not recover_one(prior_sha):
                    continue
                ignore_unknown = True
            # accessible (ou accessibilité illisible) : lecture normale, B5.
        sha = str(row.get("sha") or "").strip()

        def freeze_unknown(value, *, reachable=None):
            """Un inconnu de ce gel, avec le contexte du refus (#3835)."""
            if reachable is None and sha:
                reachable = at_ref(sha)
            return PriorFreezeUnknown(
                value, freeze_id=row_id, freeze_sha=sha, lot=lot,
                reachable=reachable, reason=access_why.get(sha, ""),
                repo=repo)

        if not sha:
            if strict:
                raise review_provenance.ProvenanceError(
                    prior_freeze_problem(
                        lot, row_id, "",
                        "names no commit: its authors cannot be carried",
                        head_sha=head_sha))
            unknown.append(freeze_unknown("freeze:%d" % row_id))
            continue
        # un gel déjà attribué par la plage courante n'a pas besoin d'objets
        if plan_git.full_commit_id(sha) in current_set:
            continue
        if not present(sha) and not recover_one(sha):
            # #2924 B7 : sans les objets, impossible de prouver qu'aucun
            # correcteur n'est en cause ; B5 refuse plutôt que de deviner.
            access = at_ref(sha)
            if strict:
                raise review_provenance.ProvenanceError(
                    prior_freeze_problem(
                        lot, row_id, sha,
                        "is absent from the repository and cannot be recovered: "
                        "its authors cannot be carried",
                        head_sha=head_sha, reachable=access,
                        reason=access_why.get(sha, ""), repo=repo))
            unknown.append(freeze_unknown(sha[:12], reachable=access))
            continue
        if base and not present(base):
            recover_one(base)
        if head_sha and not present(head_sha):
            recover_one(head_sha)
        # le contenu d'un gel déjà entré dans la cible n'est pas relu (#3116) :
        # ses commits ont été relus sous leur propre lot, jamais sous celui-ci.
        if target_sha and _is_ancestor(repo, sha, target_sha, file_cache) is True:
            continue
        if base and _is_ancestor(repo, sha, base, file_cache) is True:
            continue
        # un gel plus récent déjà traité couvre celui-ci (même histoire)
        if any(_is_ancestor(repo, sha, kept, file_cache) is True for kept in processed):
            continue
        start = _read_out(repo, "merge-base", sha, base or head_sha)
        if not start:
            access = at_ref(sha)
            if strict:
                raise review_provenance.ProvenanceError(
                    prior_freeze_problem(
                        lot, row_id, sha,
                        "is not readable against the base: its authors cannot "
                        "be carried",
                        head_sha=head_sha, reachable=access,
                        reason=access_why.get(sha, ""), repo=repo))
            unknown.append(freeze_unknown(sha[:12], reachable=access))
            continue
        earlier = _commits_between(repo, start, sha)
        if earlier is not None:
            earlier = _not_in_target(repo, earlier, target_sha, head=head_sha,
                                     cache=file_cache)
            # #3544 : l'exclusion du report se trace comme celle de la plage
            # courante, avec le gel antérieur d'où vient la plage.
            from . import provenance_scope
            earlier = provenance_scope.exclude(repo, earlier, admitted_heads,
                                               exclusions_out, carried_from=sha)
        if earlier is None:
            access = at_ref(sha)
            if strict:
                raise review_provenance.ProvenanceError(
                    prior_freeze_problem(
                        lot, row_id, sha,
                        "its commit range is unreadable: its authors cannot be "
                        "carried",
                        head_sha=head_sha, reachable=access,
                        reason=access_why.get(sha, ""), repo=repo))
            unknown.append(freeze_unknown(sha[:12], reachable=access))
            continue
        # #3837: only rewritten freezes get this extra scope proof. A squash
        # whose content is still delivered keeps every contributing author.
        if target_sha and _is_ancestor(repo, sha, head_sha) is False:
            if not delivered_read:
                delivered_read = True
                try:
                    tree = _merged_tree(repo, target_sha, head_sha)
                    delivered = _delivered_paths(repo, target_sha, tree) if tree else None
                except Exception:  # noqa: BLE001 - uncertainty retains authors
                    delivered = None
            outside = [commit for commit in earlier
                       if _is_ancestor(repo, commit, head_sha) is False
                       and _outside_delivered(repo, target_sha, commit, delivered,
                                              file_cache)]
            if outside:
                # Journal before granting an exception. Read-only/unavailable
                # storage cannot silently remove provenance from the gate.
                from . import work
                details = "; ".join(
                    "%s : %s" % (commit, ", ".join(
                        "%d lignes déjà présentes dans %s de la cible" % (count, path)
                        for path, count in file_cache.get(
                            ("delivered-proof", target_sha, commit), {}).items())
                        or "0 lignes communes au delta livré")
                    for commit in outside)
                message = ("#3837 : gel #%s (%s), contenu déjà dans la cible ; "
                           "aucune contribution de ces commits au delta livré "
                           "vers %s sur %s : %s"
                           % (row_id, sha, target_sha, head_sha,
                              details))
                try:
                    work.note(db, lot, message, once=True)
                except Exception:  # noqa: BLE001 - no trace, no exception
                    outside = []
                if exclusions_out is not None:
                    exclusions_out.extend({"commit": commit, "head": target_sha,
                                           "reason": "contenu déjà dans la cible",
                                           "carried_from": sha, "freeze_id": row_id}
                                          for commit in outside)
                earlier = [commit for commit in earlier if commit not in outside]
        processed.append(sha)
        if not earlier or all(plan_git.full_commit_id(value) in current_set
                              for value in earlier):
            continue                     # déjà attribué par la plage courante
        ancestor = _is_ancestor(repo, sha, head_sha)
        stored = (True if ancestor is True
                  else _freeze_content_preserved(repo, sha, head_sha, base))
        try:
            by_name, written, review_commits, unreadable = _prior_reading(
                db, item, row, earlier, repo, families,
                provider=provider, strict=strict, target=target_sha,
                head=head_sha, base=base, cache=file_cache,
                ignore_unknown=ignore_unknown,
                ignore_missing=lambda commit: obsolete_missing(
                    commit, stored=stored, freeze_head=sha,
                    freeze_actor=str(row.get("actor") or "")))
        except Exception as exc:  # noqa: BLE001 - un gel antérieur illisible
            access = at_ref(sha)
            if strict:
                raise review_provenance.ProvenanceError(
                    prior_freeze_problem(
                        lot, row_id, sha,
                        "the authors cannot be read (%s)" % exc,
                        head_sha=head_sha, reachable=access,
                        reason=access_why.get(sha, ""), repo=repo)) from exc
            unknown.append(freeze_unknown(sha[:12], reachable=access))
            continue
        for value in list(unreadable):
            matches = [candidate for candidate in earlier if candidate.startswith(value)]
            if len(matches) != 1:
                continue
            commit = matches[0]
            # Never replace a contradictory exact receipt with another source.
            try:
                if _commit_receipt(provider, db, commit, strict=True) is not None:
                    continue
            except Exception:  # noqa: BLE001 - unreadable receipt stays unknown
                continue
            attributed = _attributed_from_other_freeze(
                db, item, row, commit, repo, families,
                provider=provider, cache=file_cache, target=target_sha)
            if attributed is None:
                continue
            source_authors, source_written, source_corrections = attributed
            for actor, values in source_authors.items():
                by_name.setdefault(actor, set()).update(values)
                written.setdefault(actor, []).extend(source_written.get(actor, ()))
            for actor, entries in source_corrections.items():
                review_commits.setdefault(actor, []).extend(entries)
            unreadable.remove(value)
        if unreadable:
            access = at_ref(sha)
            unknown.extend(freeze_unknown(value, reachable=access)
                           for value in unreadable)
        for actor, proven in by_name.items():
            # #3335 (auditeur B1) : un relecteur promu auteur d'un gel antérieur
            # (#2924 B4) ne garde pas le raccourci d'ascendance (`stored`) : un
            # gel postérieur a pu retirer son correctif. La même preuve de
            # contenu que la trace « correcteur » décide, et un commit déjà
            # couvert par la plage courante est laissé à la lecture de cette
            # tête (qui l'a déjà filtré). Le titulaire non relecteur garde le
            # raccourci : son contenu est attribué par la plage courante.
            if actor in review_commits:
                actor_commits = [str(value) for value in written.get(actor) or ()]
                if actor_commits and all(
                        plan_git.full_commit_id(value) in current_set
                        for value in actor_commits):
                    continue
                if not survives(written, stored, actor, content=True):
                    continue
            elif not survives(written, stored, actor):
                continue
            carried.setdefault(actor, set()).update(
                value for value in proven if value)
            if commits_out is not None:
                key = next((name for name in commits_out
                            if name.lower() == actor.lower()), actor)
                values = commits_out.setdefault(key, [])
                for sha in written.get(actor) or ():
                    value = str(sha or "").strip()
                    if value and value not in values:
                        values.append(value)
        for actor in review_commits:
            # #3335 : la trace « correcteur » exige la preuve de contenu même
            # quand le gel antérieur est un ancêtre de la tête (stored) ; un
            # commit postérieur a pu retirer la correction. Les commits déjà
            # couverts par la plage courante sont filtrés par la lecture de
            # cette tête : quand ils y sont tous, elle a déjà statué et le gel
            # antérieur ne doit pas réintroduire un correcteur qu'elle a écarté.
            actor_commits = [str(value) for value in written.get(actor) or ()]
            if actor_commits and all(
                    plan_git.full_commit_id(value) in current_set
                    for value in actor_commits):
                continue
            # #3338 : une correction qui survit suffit, comme dans
            # `_surviving_review_commits` (la docstring le dit déjà) ; exiger
            # `every=True` retirait la trace d'un correcteur dont une seule
            # correction survit.
            if not survives(written, stored, actor, content=True):
                continue
            # Mesure le seuil sur les correctifs que la plage courante ne
            # couvre pas : ceux qu'elle couvre ont déjà été jugés par
            # `_surviving_review_commits` (#3335).
            prior_entries = [
                entry for entry in review_commits[actor]
                if plan_git.full_commit_id(str(
                    entry[0] if isinstance(entry, (tuple, list)) else entry))
                not in current_set]
            rows = _prior_correctors(
                repo, {actor: prior_entries}, str(item.get("app") or ""))
            for name, row in rows.items():
                if name in correctors and not correctors[name].get("test_only"):
                    row.pop("test_only", None)
                correctors[name] = row
    # #4387 (avis auditeur B1) : l'acteur d'une tête de gel dispensée est un
    # correcteur du §2, exclu **par son nom** seulement ; sa famille reste
    # éligible. Une trace déjà connue garde sa famille (ne pas l'effacer).
    for name in sorted(dispensed):
        correctors.setdefault(name, {"name": name, "family": "",
                                     "families": []})
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


#: #4457 : fenêtre de recherche du correctif autour du verdict de relecture.
#: Le relecteur corrige dans son tour : 6 h avant l'avis couvrent un tour
#: long, 2 h après couvrent la poussée qui suit l'avis. La fenêtre est lue
#: exhaustivement (B/P2 cx3), jamais tronquée à 500 reçus.
REWRITTEN_BEFORE_S = 6 * 3600
REWRITTEN_WINDOW_S = 2 * 3600
#: budget local de la lecture quand l'appelant n'a pas posé de `sweep_deadline`
#: (le gel et la désignation) : au-delà, la lecture est incomplète et lève.
REWRITTEN_SCAN_BUDGET_S = 60.0


def _signed_lines(adds, removes) -> set:
    """Les lignes significatives d'un diff, normalisées et signées (#5066)."""
    return ({("+", " ".join(line.split())) for line in adds
             if _meaningful_line(line)}
            | {("-", " ".join(line.split())) for line in removes
               if _meaningful_line(line)})


def _reissued_in_head(repo: str, commit: str, head: str, freeze: str,
                      target: str) -> bool:
    """Le contenu de `commit` a-t-il pu être réémis dans la tête (#5066) ?

    Les commits propres à la tête (ni dans le gel, ni dans la cible), avec la
    part propre de leurs résolutions de fusion, sont comparés au commit, ligne
    significative par ligne significative, y compris les lignes retirées : une
    réécriture qui remplace les mêmes lignes partage ses retraits. Une seule
    ligne commune suffit, quel que soit le volume alentour (B cx3 : un seuil se
    dilue). Sans cible, sur un diff illisible ou sans ligne significative, la
    réponse est oui (canon §2 : en cas de doute, la porte reste fermée). Le prix
    assumé : deux lots voisins qui partagent une ligne par hasard restent
    fermés (#5029/#5038 : 5 lignes). Limite : une réécriture d'ajouts purs sans
    aucune ligne commune ne se détecte pas par le contenu."""
    if not target:
        return True
    try:
        delta = _text_delta(repo, commit + "^", commit)
        proc = _read_git(repo, "log", "-p", "--diff-merges=remerge", "--no-color",
                         "--no-ext-diff", "--no-textconv", "--no-renames",
                         "--unified=0", "--format=", head, "^" + freeze,
                         "^" + target, "--")
    except (UnicodeDecodeError, canon.GitError, canon.GitDeadline,
            plan_git.MergeProbeError):
        return True
    if delta is None or proc.returncode:
        return True
    adds, removes, hunk = set(), set(), False
    for line in proc.stdout.decode("utf-8", "replace").split("\n"):
        if line.startswith("diff --git "):
            hunk = False
        elif line.startswith("@@"):
            hunk = True
        elif hunk and line.startswith("+"):
            adds.add(line[1:])
        elif hunk and line.startswith("-"):
            removes.add(line[1:])
    own = _signed_lines(adds, removes)
    if not own:
        return False
    lines = _signed_lines(*delta)
    if not lines:
        return True
    return bool(lines & own)


def _rewritten_after_freeze(db, item: Mapping, milestone: Optional[Mapping],
                            repo: str, *, head: str = "", strict: bool = False,
                            fetch: bool = True, recover=None,
                            unattached: Optional[list] = None,
                            target: str = "") -> dict[str, dict]:
    """Les correctifs de relecteur **retirés** après leur gel (#4457).

    Un relecteur désigné corrige lui-même au premier tour (canon §2) : son
    commit est poussé **après** la tête gelée, pas dedans. Quand l'auteur le
    réécrit depuis le B, le commit d'origine n'est plus ancêtre de la nouvelle
    tête : la lecture de la plage n'en voit rien, et `_carried_from_previous`
    ne le connaît pas non plus (il n'a jamais été une tête gelée). Le relecteur
    réapparaissait donc comme un candidat indépendant, alors que le §2
    l'exclut **par son nom** — sa famille, elle, redevient éligible (« un
    autre agent relit »).

    La preuve ne vient pas d'une note : le reçu exact du commit
    (`provenance_receipts`, #1465) nomme l'acteur et le SHA, et la topologie
    Git prouve le geste. Pour chaque gel antérieur du lot et chaque verdict
    rendu par un relecteur sur sa tête, on lit les commits que ce relecteur a
    créés après le gel. Un commit qui descend de la tête gelée et qui n'est
    plus ancêtre de la tête lue est le correctif réécrit : son acteur reste
    exclu par son nom, sans famille. #5066 : sauf si le reçu est rattaché à
    un autre lot dont une tête contient le commit (un lot empilé sur le gel) et
    que son contenu n'est pas réémis dans la tête (`_reissued_in_head`).

    La fenêtre lue est bornée autour du verdict (6 h avant, 2 h après, sans
    dépasser le gel courant) et **exhaustive** : une troncature silencieuse
    ferait disparaître le correctif sous des reçus plus récents (B/P2 cx3).
    L'échéance `_SWEEP` de l'appelant (ou un budget local de 60 s hors de ces
    appels) est vérifiée avant chaque lecture et chaque fetch ; une lecture
    incomplète lève, la porte refuse et la désignation attend. Un objet absent
    n'est demandé qu'une fois par hôte (`fetch_cache`) : un refus n'est pas
    redemandé à la lecture suivante.

    Un dépôt ou une tête illisible rend `{}` (la lecture garde son
    comportement historique) ; `strict` (la porte, le train) refuse au
    contraire une lecture des reçus qui échoue, comme le reste de la
    provenance. `recover`/`fetch` rapatrient l'objet du commit retiré quand
    le clone de lecture ne l'a pas (clone scratch de `merge-pr`). Un objet
    irrécupérable **rattaché à ce lot** est un doute (#4679) : l'acteur reste
    exclu par son nom, `unresolved` porte le SHA absent, et le gel le dit — on
    ne redésigne pas en silence. Un reçu rattaché à un autre lot (`work_item_id`,
    ou le tour lu dans `turn_costs`) ne concerne pas cette tête et est écarté ;
    un reçu **sans aucun rattachement** ne prouve rien sur ce lot : il n'exclut
    pas son acteur, et `unattached` reçoit sa trace pour que le gel le dise
    (#4679 B/P2 auditeur : sept exclusions sans fondement en 48 h).
    """
    item = dict(item or {})
    if db is None or not repo or packet_composer(item):
        return {}
    state_dir = str(getattr(getattr(db, "cfg", None), "state_dir", "") or "")
    lot = int(item.get("id") or 0)
    if not lot:
        return {}
    head_sha = str(head or (milestone or {}).get("sha") or "").strip().lower()
    if not head_sha:
        return {}
    sweep = _SWEEP.get()
    local = Deadline(REWRITTEN_SCAN_BUDGET_S) if sweep is None else None

    def check() -> None:
        """Borne la lecture : échéance de l'appelant, sinon budget local."""
        if sweep is not None:
            sweep.check("rewritten correctors")
        elif local is not None and local.expired():
            raise review_provenance.ProvenanceError(
                "the rewritten-corrector scan exceeded its %d s budget"
                % int(REWRITTEN_SCAN_BUDGET_S))

    def remaining() -> Optional[float]:
        """Le temps qui reste, transmis aux opérations bloquantes (#4457)."""
        if sweep is not None:
            return sweep.deadline.remaining()
        if local is not None:
            return local.remaining()
        return None

    def receipt_lot(receipt) -> Optional[int]:
        """Le lot que le reçu nomme, ou None quand il ne le nomme pas (#4679).

        `work_item_id` est NULL sur un reçu de commit du hook post-commit ; la
        ligne `turn_costs` du tour le porte alors. Toute lecture impossible
        rend None : l'attribution ne se devine pas, et un reçu sans lot
        n'exclut personne (il est dit au gel, `unattached`)."""
        work_item = (receipt or {}).get("work_item_id")
        if work_item is not None:
            try:
                return int(work_item)
            except (TypeError, ValueError):
                return None
        turn = str((receipt or {}).get("turn") or "").strip()
        if not turn:
            return None
        try:
            return storage.of(db).turn_costs.work_item_of_turn(turn)
        except Exception:  # noqa: BLE001 - lecture impossible : pas de lot
            return None

    def stacked_by_other_lot(receipt, full: str) -> bool:
        """Le commit est-il le travail d'un autre lot empilé sur ce gel (#5066) ?

        Il faut deux preuves : le reçu rattaché à un autre lot, et le commit
        contenu dans une tête de cet autre lot (sa tête de branche
        enregistrée ou l'un de ses gels). Un reçu sans lot, une tête
        illisible ou un objet absent ne prouvent rien, et l'acteur reste
        exclu (canon §2 : en cas de doute, la porte reste fermée)."""
        related = receipt_lot(receipt)
        if related is None or related == lot:
            return False
        try:
            other = storage.of(db).work.get(int(related)) or {}
            heads = [str(other.get("branch_head") or "").strip()]
            heads.extend(str(row.get("sha") or "").strip()
                         for row in storage.of(db).work.milestones(
                             int(related), None) or ()
                         if row.get("kind") == "frozen")
        except Exception:  # noqa: BLE001 - autre lot illisible : aucune preuve
            return False
        return any(value and _is_ancestor(repo, full, value) is True
                   for value in heads)

    check()
    try:
        if not _read_local_commit(repo, head_sha):
            return {}
    except Exception:  # noqa: BLE001 - tête illisible : comportement historique
        return {}
    try:
        rows = storage.of(db).work.milestones(lot, None) or []
    except Exception:  # noqa: BLE001 - frise illisible : comportement historique
        return {}
    check()
    freezes = sorted((row for row in rows if row.get("kind") == "frozen"),
                     key=lambda value: (float(value.get("at_ts") or 0.0),
                                        int(value.get("id") or 0)))
    if not freezes:
        return {}
    current_id = int((milestone or {}).get("id") or 0)
    current_at = float((milestone or {}).get("at_ts") or 0.0)
    verdicts = [row for row in rows
                if row.get("kind") == "verdict"
                and str(row.get("sha") or "").strip()]
    author = str(item.get("assignee") or "").strip().casefold()
    # nominalement : acteur -> [(gel, tête, instant du verdict)] des têtes lues
    wanted: dict[str, list] = {}
    for freeze in freezes:
        freeze_id = int(freeze.get("id") or 0)
        if current_id and freeze_id >= current_id:
            continue
        sha = str(freeze.get("sha") or "").strip().lower()
        if not sha or plan_git.same_commit(sha, head_sha):
            continue
        for verdict in verdicts:
            if not plan_git.same_commit(verdict.get("sha"), sha):
                continue
            actor = str(verdict.get("actor") or "").strip()
            key = actor.casefold()
            if not actor or key == author:
                continue
            stamp = float(verdict.get("at_ts") or freeze.get("at_ts") or 0.0)
            wanted.setdefault(key, []).append((freeze_id, sha, stamp, actor))
    check()
    if not wanted:
        return {}
    out: dict[str, dict] = {}
    #: #4679 : acteur -> reçu dont l'objet est introuvable. Ce n'est pas une
    #: absence : la topologie manque, le correcteur peut être l'auteur du
    #: commit local réécrit avant son push. On l'exclut par son nom et le gel
    #: dit le doute — mais **seulement** si le reçu est rattaché à ce lot.
    doubt: dict[str, dict] = {}
    #: #4679 B/P2 auditeur : reçu introuvable **sans lot** (tour absent du
    #: grand livre, ou plusieurs lots) : il ne prouve rien sur cette tête.
    #: On le dit au gel (`unattached`) sans exclure son acteur.
    ignored: dict[tuple[str, str], dict] = {}

    def finish(result: dict) -> dict:
        """Ajoute la trace des reçus sans lot au retour (#4679 B/P2)."""
        if unattached is not None:
            unattached.extend(ignored[key] for key in sorted(ignored))
        return result

    for key in sorted(wanted):
        for freeze_id, freeze_sha, verdict_at, actor in wanted[key]:
            since = max(0.0, verdict_at - REWRITTEN_BEFORE_S)
            until = verdict_at + REWRITTEN_WINDOW_S
            if current_at and current_at >= verdict_at:
                until = min(until, current_at)
            if until <= since:
                continue
            check()
            try:
                receipts = storage.of(db).provenance.commits(
                    actor, since_ts=since, until_ts=until)
            except Exception as exc:  # noqa: BLE001 - lecture des reçus impossible
                if strict:
                    raise review_provenance.ProvenanceError(
                        "the provenance of prior reviewer corrections cannot be read"
                        " (%s)" % exc) from exc
                check()
                for name, row in doubt.items():
                    out.setdefault(name, row)
                return finish(out)
            # #4457 (gel #25249) : une seule sonde `cat-file --batch-check`
            # pour tous les reçus, au lieu d'un processus git par SHA ; les
            # 500 reçus du témoin passent de ~40 s à moins d'une seconde.
            present = _present_commits(
                repo, [str((row or {}).get("sha") or "").strip()
                       for row in receipts or ()], timeout=remaining())
            check()
            for receipt in receipts or ():
                check()
                sha = str((receipt or {}).get("sha") or "").strip().lower()
                if not sha:
                    continue
                if present is None:
                    # Lecture groupée impossible : repli par SHA, jamais une
                    # absence supposée.
                    try:
                        full = _read_local_commit(repo, sha)
                    except Exception:  # noqa: BLE001 - objet illisible : ignoré
                        full = None
                else:
                    full = present.get(sha)
                if not full and not fetch_cache.refused(
                        repo, sha, state_dir=state_dir):
                    refused = False
                    if recover is not None:
                        check()
                        try:
                            if recover(sha, timeout=remaining()):
                                full = sha
                        except Exception:  # noqa: BLE001 - source en échec
                            pass
                    if not full and fetch:
                        check()
                        try:
                            outcome = _forge_fetch(repo, sha, timeout=remaining())
                        except Exception:  # noqa: BLE001 - forge injoignable
                            outcome = None
                        # `_forge_fetch` rend l'objet trouvé (True), un refus
                        # net (False) ou un doute (None) : une seule sonde, pas
                        # de `local_commit` par SHA après un échec (#25249).
                        if outcome is True:
                            full = sha
                        # Seul un refus **net** de git fetch se mémorise ; une
                        # coupure par l'échéance ou une exception reste un
                        # doute qui se relit (#4457 B/P2 auditeur).
                        refused = outcome is False
                    # L'appel bloquant a pu consommer toute l'échéance : le
                    # constat d'absence ne doit pas l'absorber, et on ne note
                    # rien sous une lecture expirée (#4457 B/P2 cx3/auditeur).
                    check()
                    if refused and not full:
                        fetch_cache.note(repo, sha, state_dir=state_dir)
                if not full:
                    # #4679 : le reçu peut nommer le commit **local** d'avant
                    # un rebase/amend, jamais poussé : aucun clone ne le
                    # servira, et le commit poussé n'a pas de reçu. L'ignorer
                    # redésignait le correcteur en silence. Seul le reçu
                    # **rattaché à ce lot** est un doute : un reçu d'un autre
                    # lot ne dit rien de cette tête, et un reçu sans aucun
                    # rattachement ne prouve rien (B/P2 auditeur) — il est dit
                    # au gel, jamais exclu.
                    related = receipt_lot(receipt)
                    if related == lot:
                        doubt.setdefault(key, {
                            "name": actor, "family": "", "families": [],
                            "unresolved": sha, "freeze": freeze_id})
                    elif related is None and unattached is not None:
                        ignored.setdefault((key, sha), {
                            "name": actor, "sha": sha, "freeze": freeze_id})
                    continue
                check()
                if _is_ancestor(repo, full, head_sha) is True:
                    continue                # encore dans la tête : lu par la plage
                if _is_ancestor(repo, freeze_sha, full) is not True:
                    continue
                # #5066 : la descendance du gel ne prouve pas une correction
                # de ce lot. Un lot empilé sur ce gel (#5038 sur le gel
                # fe75128d3 de #5029) en descend aussi. Le reçu n'est écarté
                # que sur trois preuves : il est rattaché à un autre lot, le
                # commit est contenu dans une tête de cet autre lot, et son
                # contenu n'est pas réémis ici (B cx2, contre-examen #173774).
                check()
                if (stacked_by_other_lot(receipt, full)
                        and not _reissued_in_head(repo, full, head_sha,
                                                  freeze_sha, target)):
                    continue
                # Un seul nom par acteur ; la première tête gelée qui porte
                # le correctif suffit à la trace.
                out.setdefault(key, {"name": actor, "family": "",
                                     "families": [],
                                     "rewritten": full,
                                     "freeze": freeze_id})
                break
            if key in out:
                break
    check()
    # Une preuve topologique, quand elle existe, garde la trace la plus forte ;
    # le doute ne complète que les acteurs sans preuve.
    for name, row in doubt.items():
        out.setdefault(name, row)
    return finish(out)


def reading(db, item: Mapping, milestone: Optional[Mapping],
            commits: Sequence[str], repo: str = "", families=(),
            *, reference: Optional[Mapping] = None,
            reference_families: Optional[Mapping] = None, provider=None,
            strict: bool = False, trace: bool = False,
            families_out: Optional[dict] = None,
            base: str = "", head: str = "", target: str = "",
            recover=None, admitted_heads=(),
            exclusions_out: Optional[list] = None,
            commits_out: Optional[dict] = None,
            fetch: bool = True,
            milestone_proof: Optional[Mapping] = None,
            doubts_out: Optional[list] = None) -> tuple[list, list, list]:
    """`(auteurs, inconnus, correcteurs)` de la plage gelée, même lecture pour tous.

    `milestone_proof` (#3948) est la preuve du jalon lue hors transaction,
    avant son INSERT : elle tient lieu de reçu du gel courant pour
    [`reference_author`]. Un rejeu ou la porte laissent le défaut (`None`) et
    lisent le reçu en base.

    `reference` est l'auteur de référence déjà lu (`reference_author`) : la
    désignation et le train la partagent. `reference_families` porte les
    familles prouvées de cette référence (le `families_out` de
    `reference_author`, #2961) : deux familles différentes sont une ambiguïté
    et sont ajoutées aux preuves de l'auteur. `provider` est le fournisseur
    exact de reçus (#1465) ; `strict` fait d'un reçu corrompu une erreur (le
    train), au lieu d'une absence (la désignation attend). `trace=True`
    (désignation) ajoute aux auteurs du repli `proof: turn-journal` et
    `fallback_commits`. Une entrée est `{name, family}`, plus `families` quand
    plusieurs familles sont prouvées (#3584) : le train et la porte reçoivent
    alors tout l'ensemble, jamais une famille devinée. `families_out` reçoit,
    par acteur en minuscules, l'ensemble des familles **prouvées** : une
    famille vide peut être une absence de preuve ou une ambiguïté, et
    l'appelant qui doit les distinguer ne peut pas la deviner.

    `commits_out` (#3742) associe à chaque acteur des listes de SHA que cette
    lecture lui attribue : la plage courante, puis les commits des gels
    antérieurs qui ont porté sa famille jusqu'à cette tête. Il ne sert qu'à
    nommer les commits d'un refus de gel ; aucune décision ne le lit, et son
    absence ne change rien.

    Un commit sans reçu est attribué au committer d'adresse d'agent : un commit
    du titulaire de référence reste le sien ; un relecteur désigné reste
    l'auteur de sa correction, avec sa famille de session ; un tiers non
    relecteur reste inconnu. #3805 : quand l'auteur Git est un agent du
    registre différent du committer, les deux familles sont auteurs du contenu
    (le doute conserve) ; une réémission à patch-id verbatim identique d'un
    commit qui porte un reçu exact garde ce reçu et n'est pas attribuée au
    committer. Le seuil du §2 ne décide que de la trace « correctif
    de premier tour », jamais de l'auteur (#2888).

    Les **correcteurs** (#2739) sont les relecteurs désignés dont le correctif
    de premier tour reste sous le seuil. Ce sont des **auteurs de la tête**
    (#2888) : leur famille est exclue comme celle de tout auteur, et l'agent
    lui-même ne relit pas son propre correctif. `correctors` garde leur trace
    (provenance réelle) pour l'exclusion personnelle ; ce n'est plus une
    éligibilité de famille. Exception #3518/#3577 : un correctif de tests
    seuls, même sous un chemin sensible, reste dans `correctors` avec
    `test_only`, quelle que soit sa taille, mais n'ajoute pas un auteur
    produit. La désignation ignore sa famille et exclut toujours son nom. Le
    report des gels antérieurs garde cette distinction. Un correctif au-delà
    du seuil ou de conception est aussi un auteur, mais n'est pas un
    « correcteur » de premier tour. #4457 : un correctif de relecteur poussé
    **après** un gel et réécrit par l'auteur n'est ni dans la tête ni dans un
    gel ; le reçu exact du commit retiré et la topologie le retrouvent, et son
    acteur reste dans `correctors` avec une famille vide (`rewritten` nomme le
    commit). Exclu par son nom seulement, il laisse sa famille éligible : un
    autre agent relit (canon §2). #4679 : si le reçu ne nomme que le commit
    **local** d'avant un rebase/amend, l'objet est introuvable ; le doute
    garde l'exclusion (`unresolved` nomme le SHA) et le gel le dit, au lieu de
    redésigner en silence. Ce doute ne vaut que pour un reçu **rattaché à ce
    lot** (`work_item_id` ou tour) : un reçu sans rattachement ne prouve rien
    ici, `doubts_out` reçoit sa trace (`{name, sha, freeze}`) pour que le gel
    le dise, sans exclure son acteur (#4679 B/P2 auditeur).
    #3335 : cette promotion et la trace « correcteur » suivent la survie du
    contenu (#3220). Un correctif de relecteur dont aucune ligne significative
    ne subsiste plus dans la tête n'exclut plus sa famille ; une ligne qui
    survit, ou une lecture de contenu impossible, la garde exclue.

    #4179 : la famille d'un auteur **tiers** suit la même survie de contenu.
    Un commit hérité d'un parent empilé puis retiré par un commit postérieur de
    la tête (revert, annulation) n'est pas un contenu de cette tête : le
    compter comme auteur fermerait des familles à tort. Une ligne qui survit,
    ou une lecture impossible, garde l'exclusion. La référence du lot garde sa
    lecture (#2808) ; les auteurs des gels antérieurs suivent
    `_carried_from_previous` (#2924).

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

    #3513 : `admitted_heads` sont les gels des membres déjà admis par la
    porte du même véhicule. Leurs commits sont écartés de la plage courante
    et des gels antérieurs, comme ceux de la cible ; aucun autre tiers
    ne bénéficie de cette exclusion. #4478 : les têtes gelées des parents
    d'empilement (`stacked_parent_heads`) sont la même borne pour un lot
    empilé. Une lecture Git impossible ne retire rien.
    Une portée est un gel (texte) ou la portée de `provenance_scope` (dict,
    #3533). #3544 : `exclusions_out` reçoit les commits écartés du report des
    gels antérieurs (`carried_from` = le gel reporté) ; la plage courante est
    tracée par la porte.

    #3116 : `target` est la cible au moment de la lecture (commit ou ref). Un
    commit qui y est déjà entré ne compte ni comme auteur ni comme tiers du
    lot, qu'il vienne de la plage courante ou du report d'un gel antérieur :
    il a été relu sous son propre lot. Sans `target`, `base` — toujours un
    ancêtre de la cible — sert de borne sûre ; sans l'un ni l'autre, la
    lecture garde son comportement d'avant (aucun commit écarté).

    `fetch=False` (#3836) ne rapatrie rien : c'est la lecture d'une
    transaction de gel, qui ne doit jamais attendre le réseau en tenant ses
    verrous. Les objets des gels antérieurs se préparent hors transaction par
    `prefetch_prior_freezes`.
    """
    item = dict(item or {})
    packet = bool(packet_composer(item))
    target_sha = target_commit(repo, target, fetch=fetch) or target_commit(
        repo, base, fetch=fetch)
    commits = _not_in_target(repo, commits, target_sha,
                             head=str(head or (milestone or {}).get("sha") or ""))
    for admitted in admitted_heads:
        commits = _not_in_target(repo, commits, admitted["head"]
                                 if isinstance(admitted, dict) else admitted)
    proven_reference: dict = dict(reference_families or {})
    if reference is None:
        reference = reference_author(db, item, milestone, families,
                                     provider=provider, strict=strict,
                                     families_out=proven_reference,
                                     receipt=milestone_proof)
    author_name = str((reference or {}).get("name") or "").strip()
    author_family = str((reference or {}).get("family") or "")
    reviewers = designated_reviewers(db, item, milestone, author_name)
    by_name: dict[str, set] = {}
    unknown: list[str] = []
    review_commits: dict[str, list[tuple[str, str]]] = {}
    # #3805 : familles d'une correction reçue sous plusieurs harnais, par
    # commit ; la promotion en auteur ne garde que les commits survivants.
    review_families: dict[str, dict[str, set]] = {}
    proofs: dict[str, set[str]] = {}
    # #4179 : les commits d'un tiers de la plage courante, pour ne garder que
    # ceux dont le contenu survit dans la tête (un parent empilé puis retiré
    # n'est pas un auteur de cette tête).
    third_commits: dict[str, list[str]] = {}
    # #3805 : les copies d'une même réémission partagent la liste des commits
    # candidats d'un auteur (une seule recherche `git log --all` par auteur).
    patch_receipt_cache: dict = {}

    def record_commit(actor: str, sha: str) -> None:
        """#3742 : le commit attribué à un acteur, pour nommer un refus de gel."""
        if commits_out is None or not actor or not sha:
            return
        key = next((name for name in commits_out
                    if name.lower() == actor.lower()), actor)
        values = commits_out.setdefault(key, [])
        if sha not in values:
            values.append(sha)

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
            rows, missing = _unreceived_authors(
                db, item, repo, sha, author_name=author_name,
                author_family=author_family, reviewers=reviewers,
                families=families, provider=provider,
                cache=patch_receipt_cache)
            unknown.extend(missing)
            for row in rows:
                actor = row["name"]
                values = {str(value) for value in row.get("families") or ()
                          if str(value or "").strip()}
                if row["family"]:
                    values.add(str(row["family"]))
                if row["role"] == "reviewer":
                    review_commits.setdefault(actor, []).append(
                        (sha, row["family"]))
                    review_families.setdefault(actor, {}).setdefault(
                        sha, set()).update(values)
                else:
                    by_name.setdefault(actor, set()).update(values)
                    if row["role"] == "third":
                        third_commits.setdefault(actor, []).append(sha)
                    record_commit(actor, sha)
                if row["proof"]:
                    proofs.setdefault(actor, set()).add(sha[:12])
            continue
        actor = str(receipt.get("actor") or "").strip()
        if not actor:
            unknown.append(sha[:12])
            continue
        proven = _family(receipt.get("harness"), families)
        if actor == author_name:
            by_name.setdefault(actor, set()).add(proven)
            record_commit(actor, sha)
        elif actor in reviewers:
            # sa correction est un contenu de la tête (#2888) ; le seuil ne
            # décide que de la trace « correcteur de premier tour »
            review_commits.setdefault(actor, []).append((sha, proven))
        else:
            by_name.setdefault(actor, set()).add(proven)
            third_commits.setdefault(actor, []).append(sha)
            record_commit(actor, sha)
    # #4179 : la famille d'un auteur tiers ne s'exclut que par le contenu qui
    # survit dans la tête (#3335/#3220). Un commit hérité d'un parent empilé
    # puis retiré (revert, annulation) n'est pas un contenu de cette tête : le
    # compter comme auteur tiers fermerait des familles à tort. La référence
    # garde sa lecture (#2808) et les correcteurs la leur ci-dessous.
    third_surviving = _surviving_author_commits(
        repo, head, base, third_commits, {})
    for actor, shas in third_commits.items():
        if author_name and str(actor).casefold() == author_name.casefold():
            continue
        kept = third_surviving.get(actor) or []
        if not kept:
            by_name.pop(actor, None)
            if commits_out is not None:
                for key in [name for name in commits_out
                            if name.lower() == actor.lower()]:
                    commits_out.pop(key, None)
            continue
        if commits_out is not None and set(kept) != set(shas):
            for key in [name for name in commits_out
                        if name.lower() == actor.lower()]:
                commits_out[key] = list(kept)
    # #3335 : un correctif de relecteur ne pèse que s'il est encore dans la
    # tête. La promotion en auteur (#2888) et la trace « correcteur » suivent la
    # survie du contenu (#3220) : un commit de relecteur dont aucune ligne ne
    # subsiste (retiré par un commit postérieur de la même plage) n'exclut plus
    # sa famille. Une lecture impossible garde le commit, donc l'exclusion.
    review_commits = _surviving_review_commits(
        repo, head, base, review_commits, {})
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
            written = commit_ids_written_by(repo, commits, author_name, db)
            if written is None:
                unknown.extend(str(sha)[:12] for sha in commits if str(sha)[:12] not in unknown)
            elif written:
                authored = author_name
                by_name.setdefault(authored, set())
                for sha in written:
                    record_commit(authored, sha)
        if authored:
            for family in proven_reference.get(author_name.lower()) or ():
                if family:
                    by_name[authored].add(family)
            if author_family:
                by_name[authored].add(author_family)
    # #2888 : toute correction d'un relecteur désigné fait de lui un auteur de
    # la tête, quelle que soit sa taille. Le seuil du §2 ne décide que de la
    # recevabilité process (une correction trop grosse revient à l'auteur),
    # jamais de l'indépendance. #3518/#3577 : les tests seuls font exception à
    # l'exclusion de famille, même sous un chemin sensible ; le nom reste exclu.
    small_correctors: set[str] = set()
    test_correctors: set[str] = set()
    for actor, entries in sorted(review_commits.items()):
        total, design = 0, False
        for sha, _proven in entries:
            measured = change(repo, sha)
            if measured is None or measured[1]:
                design = True
                break
            total += measured[0]
        if _test_only_correction(repo, entries, str(item.get("app") or "")):
            test_correctors.add(actor)
        else:
            by_name.setdefault(actor, set())
            for sha, proven in entries:
                if proven:
                    by_name[actor].add(proven)
                # #3805 : une correction reçue sous plusieurs harnais garde
                # toutes ses familles, pas la première de `git log`.
                by_name[actor].update(
                    review_families.get(actor, {}).get(sha) or ())
                record_commit(actor, sha)
        if actor in test_correctors or (not design and total <= REVIEWER_FIX_LINES):
            small_correctors.add(actor)
    # #2924 : les auteurs des gels antérieurs dont le contenu subsiste restent
    # auteurs de cette tête. Une réécriture à contenu identique ne les efface
    # pas ; un contenu réellement réécrit n'est pas repris (le doute conserve).
    carried, carried_unknown, carried_correctors = _carried_from_previous(
        db, item, milestone, commits, repo, families, base=base, head=head,
        provider=provider, strict=strict, target=target_sha, recover=recover,
        admitted_heads=admitted_heads, exclusions_out=exclusions_out,
        commits_out=commits_out, fetch=fetch, reference_name=author_name)
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
        # un acteur à deux familles prouvées est ambigu : jamais deviné, et
        # l'ensemble est transporté pour que la désignation et la porte en
        # excluent chacune (#3584), au lieu de le lire comme une famille
        # inconnue.
        family = next(iter(proven)) if len(proven) == 1 else ""
        row = {"name": actor, "family": family}
        if len(proven) > 1:
            row["families"] = sorted(proven)
        if trace and proofs.get(actor):
            # #2721 : la trace est celle de la désignation (`trace=True`) ;
            # l'ensemble `families` reste porté par la ligne (#3584).
            row["proof"] = "turn-journal"
            row["fallback_commits"] = sorted(proofs[actor])
        out.append(row)
    corrector_rows: dict[str, dict] = {}

    def record_corrector(name: str, family: str, families_values=(),
                         *, test_only=False) -> None:
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
            row = {"name": name, "family": "", "proven": set(),
                   "test_only": True}
            corrector_rows[key] = row
        row["test_only"] = row["test_only"] and test_only
        row["proven"].update(proven)
        # une ambiguïté de familles ne change rien à l'exclusion par le nom,
        # mais elle ne doit pas être présentée comme une famille unique.
        row["family"] = (next(iter(row["proven"]))
                         if len(row["proven"]) == 1 else "")
        if families_out is not None:
            families_out.setdefault(key, set()).update(row["proven"])

    for actor in sorted(small_correctors):
        proven = {value for _sha, value in review_commits[actor] if value}
        for sha, _value in review_commits[actor]:
            proven |= review_families.get(actor, {}).get(sha, set())
        record_corrector(actor, "", proven, test_only=actor in test_correctors)
    # #2924 : un correcteur d'un gel antérieur dont le correctif subsiste garde
    # sa trace. Hors tests seuls (#3518), il est aussi auteur de la tête ;
    # une correction produit ancienne ne devient jamais une dispense de famille.
    for row in carried_correctors:
        record_corrector(str(row.get("name") or ""),
                         str(row.get("family") or ""),
                         row.get("families") or (),
                         test_only=bool(row.get("test_only")))
    # #4457 : un correctif de relecteur poussé **après** la tête gelée puis
    # réécrit par l'auteur n'est dans aucune plage ni aucun gel : son acteur
    # reste exclu par son nom, sans famille (le §2 laisse un autre agent de la
    # même famille relire). Une trace de correcteur survivante garde, elle, sa
    # famille : ne jamais l'effacer.
    unattached: list = []
    for row in _rewritten_after_freeze(
            db, item, milestone, repo, head=head, strict=strict,
            fetch=fetch, recover=recover,
            unattached=(unattached if doubts_out is not None else None),
            target=target_sha).values():
        key = str(row.get("name") or "").strip().lower()
        if not key or key in corrector_rows:
            continue
        corrector_rows[key] = {"name": row["name"], "family": "",
                               "proven": set(), "test_only": False,
                               "rewritten": str(row.get("rewritten") or ""),
                               "unresolved": str(row.get("unresolved") or ""),
                               "freeze": int(row.get("freeze") or 0)}
    if doubts_out is not None:
        # #4679 B/P2 : reçus introuvables sans lot, à dire au gel — jamais des
        # correcteurs : ils n'entrent ni dans `corrector_rows` ni dans la porte.
        doubts_out.extend(unattached)
    correctors = []
    for row in corrector_rows.values():
        entry = {"name": row["name"], "family": row["family"]}
        if row["test_only"]:
            entry["test_only"] = True
        if row.get("rewritten"):
            entry["rewritten"] = row["rewritten"]
            if row.get("freeze"):
                entry["freeze"] = int(row["freeze"])
        if row.get("unresolved"):
            # #4679 : le commit local d'avant rebase/amend est introuvable ;
            # le nom reste exclu, le gel peut dire le doute et son SHA.
            entry["unresolved"] = row["unresolved"]
            if row.get("freeze"):
                entry["freeze"] = int(row["freeze"])
        correctors.append(entry)
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
                # #4299 : le reçu signé garde la graphie d'avant un renommage,
                # le jalon porte la nouvelle ; même fiche du registre = même acteur.
                proof_actor = str(receipt.get("actor") or "").strip()
                if (proof_actor.lower() != key
                        and not storage.of(db).agents.same_identity(proof_actor, actor)):
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
