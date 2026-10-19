# SPDX-License-Identifier: AGPL-3.0-only
"""Une seule lecture des auteurs d'une tête gelée (#2681).

La porte de fusion (`merge_queue.review_gate`) compare les avis à tous les
auteurs que l'appelant lui donne (#2681) ; la désignation au gel
(`review_designation_activation`) et le train (#1467) doivent lire les mêmes
auteurs, avec les mêmes exceptions du canon §2 :

* l'auteur de référence est l'assigné au gel (`review_lot._author_at_freeze`,
  la même trace historique que la porte) ;
* chaque commit du contenu est attribué par son reçu exact (#1465) ;
* un correctif de premier tour d'un **relecteur désigné** sous le seuil du §2
  (`REVIEWER_FIX_LINES`, pas de conception) ne fait pas de son auteur un auteur
  de la tête : sa famille reste éligible (#2638) ; l'agent lui-même est rendu
  comme **correcteur**, pour que la désignation et la porte écartent son nom
  sans exclure sa famille (#2758) ;
* quand le reçu exact manque (commit restauré par rebase, correction poussée
  hors du hook), le commit est attribué à la famille de session de son
  committer, lue au journal des tours (`turn_costs`, `_turns_harness`) : un
  commit du relecteur désigné sous le seuil reste une correction de relecteur,
  un commit de l'assigné reste l'auteur. Un tiers non relecteur sans reçu reste
  inconnu : aucun camp ne le suppose.

Le module ne devine jamais une famille depuis un nom Git seul : le committer
doit porter l'adresse d'agent (`<nom>@ameesh.invalid`) et la famille vient du
journal des tours. Un reçu exact contradictoire est une erreur, pas une
absence.
"""
from __future__ import annotations

from typing import Mapping, Optional, Sequence

from . import plan_git, registry, review_lot, review_provenance

#: canon §2 : une correction de premier tour va jusqu'à ~50 lignes changées
REVIEWER_FIX_LINES = 50
#: canon §2 « ou de conception » : un document de conception n'est jamais petit
DESIGN_PREFIXES = ("docs/design/",)


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
                     *, provider=None, strict: bool = False) -> dict:
    """L'auteur de référence `{name, family}` : l'assigné au gel.

    La famille vient de la trace du gel (`_author_at_freeze`), sinon du reçu du
    jalon quand son acteur est bien l'auteur, sinon de la session ouverte du
    registre. C'est exactement la référence de la porte (`assignee`) ; le
    registre courant ne prouve une famille que lorsque le gel ne la donne pas.
    """
    item = dict(item or {})
    lot = int(item.get("id") or 0)
    assignee = str(item.get("assignee") or "").strip()
    author = {}
    try:
        author = review_lot._author_at_freeze(db, lot, item) or {}
    except Exception:  # noqa: BLE001 - trace illisible : famille inconnue
        author = {}
    name = str(author.get("name") or assignee).strip()
    family = _family(author.get("harness"), families)
    if not family and milestone is not None:
        try:
            proof = _milestone_receipt(provider, db, int(milestone["id"]),
                                       strict=strict)
        except (review_provenance.ProvenanceError, KeyError, TypeError, ValueError):
            proof = None
        if proof and str(proof.get("actor") or "").strip() == name:
            family = _family(proof.get("harness"), families)
    if not family and name and (milestone is None
                                or str(milestone.get("actor") or "").strip()
                                in ("", name)):
        try:
            row = registry.get(db, name) or {}
            # le harnais déclaré n'est pas une session : seule une session
            # ouverte prouve la famille qui a réellement tourné (L242b, #1037)
            if row.get("session_id") or row.get("session_harness"):
                family = _family(row.get("session_harness"), families)
        except Exception:  # noqa: BLE001 - pas de session : famille inconnue
            family = ""
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


def attribution(db, item: Mapping, milestone: Optional[Mapping],
                commits: Sequence[str], repo: str = "", families=(),
                *, reference: Optional[Mapping] = None, provider=None,
                strict: bool = False) -> tuple[list, list, list]:
    """`(auteurs, inconnus, correcteurs)` de la plage gelée, même lecture.

    `reference` est l'auteur de référence déjà lu (`reference_author`) : la
    désignation et le train la partagent. `provider` est le fournisseur exact
    de reçus (#1465) ; `strict` fait d'un reçu corrompu une erreur (le train),
    au lieu d'une absence (la désignation attend).

    Un commit sans reçu est attribué au committer d'adresse d'agent : l'auteur
    de référence reste l'auteur ; un relecteur désigné sous le seuil reste un
    correctif de relecture ; un tiers non relecteur reste inconnu. Au-delà du
    seuil (ou pour une conception), le relecteur devient auteur de sa
    correction, avec sa famille de session.

    `correctors` sont les relecteurs désignés dont la correction de premier
    tour reste sous le seuil du §2 : leur **famille** reste éligible (#2638),
    mais l'agent lui-même a écrit un commit de la plage. La désignation et la
    porte l'excluent par son nom, jamais par sa famille (#2758).
    """
    item = dict(item or {})
    if reference is None:
        reference = reference_author(db, item, milestone, families,
                                     provider=provider, strict=strict)
    author_name = str((reference or {}).get("name") or "").strip()
    author_family = str((reference or {}).get("family") or "")
    reviewers = designated_reviewers(db, item, milestone, author_name)
    by_name: dict[str, set] = {}
    if author_name:
        by_name.setdefault(author_name, set()).add(author_family)
    unknown: list[str] = []
    review_commits: dict[str, list[tuple[str, str]]] = {}
    for raw in commits or ():
        sha = str(raw or "").strip()
        if not sha:
            continue
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
            if actor == author_name:
                proven = author_family or actor_family(db, actor, stamp, families)
                by_name.setdefault(actor, set()).add(proven)
                continue
            if actor in reviewers:
                proven = actor_family(db, actor, stamp, families)
                review_commits.setdefault(actor, []).append((sha, proven))
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
            # tenu jusqu'au seuil connu pour toute sa correction
            review_commits.setdefault(actor, []).append((sha, proven))
        else:
            by_name.setdefault(actor, set()).add(proven)
    for actor, entries in sorted(review_commits.items()):
        total, design = 0, False
        for sha, _proven in entries:
            measured = change(repo, sha)
            if measured is None or measured[1]:
                design = True
                break
            total += measured[0]
        if design or total > REVIEWER_FIX_LINES:
            # au-delà du petit correctif : ils ont écrit cette tête
            by_name.setdefault(actor, set())
            for _sha, proven in entries:
                if proven:
                    by_name[actor].add(proven)
    correctors = []
    for actor in sorted(review_commits):
        if actor in by_name:
            # above the threshold (or a design change): already an author,
            # their family is forbidden (#2638)
            continue
        proven = {value for _sha, value in review_commits[actor] if value}
        # an ambiguity of families does not matter here: only the name excludes
        family = next(iter(proven)) if len(proven) == 1 else ""
        correctors.append({"name": actor, "family": family})
    out = []
    for actor in sorted(by_name):
        proven = {value for value in by_name[actor] if value}
        # un acteur à deux familles prouvées est ambigu : jamais deviné
        family = next(iter(proven)) if len(proven) == 1 else ""
        out.append({"name": actor, "family": family})
    return out, unknown, correctors


def authors(db, item: Mapping, milestone: Optional[Mapping],
            commits: Sequence[str], repo: str = "", families=(),
            *, reference: Optional[Mapping] = None, provider=None,
            strict: bool = False) -> tuple[list, list]:
    """`(auteurs, inconnus)`, la lecture `attribution` sans les correcteurs.

    Les lecteurs qui n'ont pas besoin de la liste des correcteurs gardent cette
    forme ; la désignation, la porte et le train lisent `attribution` (#2758)."""
    found, unknown, _correctors = attribution(
        db, item, milestone, commits, repo, families, reference=reference,
        provider=provider, strict=strict)
    return found, unknown


def verdict_families(db, marks, families, *, provider=None,
                     strict: bool = False) -> dict[str, str]:
    """Les familles historiques des relecteurs d'une tête gelée.

    Pour chaque acteur, seul son **dernier** verdict de la liste compte (la
    porte lit ce même dernier avis) : une preuve d'un avis supplanté ne peut
    ni refuser ni changer sa famille. Le reçu exact du jalon est la preuve ;
    sans lui, le journal des tours à l'instant du verdict. Une preuve
    contradictoire est une erreur si `strict`, une absence sinon. Le résultat
    est indexé par acteur en minuscules, la casse de la porte.
    """
    latest: dict[str, dict] = {}
    for mark in marks or ():
        if mark.get("kind") != "verdict":
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
