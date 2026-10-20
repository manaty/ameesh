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
* un correctif de premier tour d'un **relecteur désigné** sous le seuil du §2
  (`REVIEWER_FIX_LINES`, pas de conception) ne fait pas de son auteur un auteur
  de la tête : sa famille reste éligible, et il peut être redésigné (#2638) ;
* quand le reçu exact manque (commit restauré par rebase, correction poussée
  hors du hook), le commit est attribué à la famille de session de son
  committer, lue au journal des tours (`turn_costs`, `_turns_harness`) : un
  commit du relecteur désigné sous le seuil reste une correction de relecteur,
  un commit de l'assigné reste l'auteur. Un tiers non relecteur sans reçu reste
  inconnu : aucun camp ne le suppose. #2721 : à la désignation, ce repli porte
  sa trace (`proof: turn-journal`, `fallback_commits`), et un committer distinct
  de la référence doit être au registre.

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
                     *, provider=None, strict: bool = False) -> dict:
    """L'auteur de référence `{name, family}` : l'assigné au gel.

    La famille vient de la trace du gel (`_author_at_freeze`), sinon du reçu du
    jalon quand son acteur est bien l'auteur, sinon de la session ouverte du
    registre. C'est exactement la référence de la porte (`assignee`) ; le
    registre courant ne prouve une famille que lorsque le gel ne la donne pas.

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


def reading(db, item: Mapping, milestone: Optional[Mapping],
            commits: Sequence[str], repo: str = "", families=(),
            *, reference: Optional[Mapping] = None, provider=None,
            strict: bool = False, trace: bool = False,
            families_out: Optional[dict] = None) -> tuple[list, list, list]:
    """`(auteurs, inconnus, correcteurs)` de la plage gelée, même lecture pour tous.

    `reference` est l'auteur de référence déjà lu (`reference_author`) : la
    désignation et le train la partagent. `provider` est le fournisseur exact
    de reçus (#1465) ; `strict` fait d'un reçu corrompu une erreur (le train),
    au lieu d'une absence (la désignation attend). `trace=True` (désignation)
    ajoute aux auteurs du repli `proof: turn-journal` et `fallback_commits` ;
    le train et la porte gardent la forme exacte `{name, family}`.
    `families_out` reçoit, par acteur en minuscules, l'ensemble des familles
    **prouvées** : une famille vide peut être une absence de preuve ou une
    ambiguïté, et l'appelant qui doit les distinguer ne peut pas la deviner.

    Un commit sans reçu est attribué au committer d'adresse d'agent : un commit
    du titulaire de référence reste le sien ; un relecteur désigné sous le
    seuil reste un correctif de relecture ; un tiers non relecteur reste
    inconnu. Au-delà du seuil (ou pour une conception), le relecteur devient
    auteur de sa correction, avec sa famille de session.

    Les **correcteurs** (#2739) sont les relecteurs désignés dont le correctif
    de premier tour reste sous le seuil : ils ne sont pas auteurs de la tête
    (#2638), mais l'agent lui-même ne relit pas son propre correctif — la
    désignation l'écarte et la porte refuse son avis, sans exclure sa famille.

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
    """
    item = dict(item or {})
    packet = bool(packet_composer(item))
    if reference is None:
        reference = reference_author(db, item, milestone, families,
                                     provider=provider, strict=strict)
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
            # tenu jusqu'au seuil connu pour toute sa correction
            review_commits.setdefault(actor, []).append((sha, proven))
        else:
            by_name.setdefault(actor, set()).add(proven)
    # #2808 : le titulaire n'est un auteur que par un commit de la plage.
    # Quand il en a écrit un (reçu/committer, ou auteur Git conservé par un
    # amend), sa famille de gel reste une preuve historique, en plus de celle
    # du commit : deux familles prouvées sont ambiguës et font attendre
    # (#2755). Un titulaire sans commit n'ajoute ni nom ni famille.
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
        if authored and author_family:
            by_name[authored].add(author_family)
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
    correctors = []
    for actor in sorted(review_commits):
        if actor in by_name:
            continue
        proven = {value for _sha, value in review_commits[actor] if value}
        if families_out is not None:
            families_out.setdefault(actor.lower(), set()).update(proven)
        # une ambiguïté de familles ne change rien ici : seul le nom exclut
        family = next(iter(proven)) if len(proven) == 1 else ""
        correctors.append({"name": actor, "family": family})
    return out, unknown, correctors


def authors(db, item: Mapping, milestone: Optional[Mapping],
            commits: Sequence[str], repo: str = "", families=(),
            *, reference: Optional[Mapping] = None, provider=None,
            strict: bool = False, trace: bool = False) -> tuple[list, list]:
    """`(auteurs, inconnus)` — la lecture partagée, sans les correcteurs."""
    authors_, unknown, _correctors = reading(
        db, item, milestone, commits, repo, families, reference=reference,
        provider=provider, strict=strict, trace=trace)
    return authors_, unknown


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
