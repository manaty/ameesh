# SPDX-License-Identifier: AGPL-3.0-only
"""Merge queue per target (lot L160e1, study L160 § 3.3).

    ameesh work queue add <lot>       the lot is ready: into its target's queue
    ameesh work queue remove <lot>    out of the queue (to push again, or give up)
    ameesh work queue show [--target T] [--json]

A lot enters the queue of its target (`branch_target`, else the repository's
default target) when it is ready, which replaces the "merge light" given by
hand:

* frozen: a `frozen` milestone with its commit (`ameesh work milestone <lot>
  frozen --sha …`), and the branch still at that commit;
* review verdict `ok` on that very commit (`ameesh work milestone <lot>
  verdict --verdict ok --sha …`);
* CI green when the branch has an open PR (a team that merges locally has
  none: nothing to check).

A queued lot is frozen: its agent no longer pushes to its branch without
taking it out first. At each pass (`radar.run_pass`), for each queue:

1. a lot whose branch moved past its freeze is ejected; a lot that merged
   is `landed`;
2. the queue is ordered: priority first, then the lots without conflict with
   another queued lot, then those whose conflicts are prepared (L160d), then
   arrival. A lot whose conflict with a lot ahead of it is not prepared waits
   behind it, and the resolver of that pair is woken once: the conflict now
   blocks;
3. the queue is replayed stacked in the radar's private clone: the target,
   plus the lots ahead, plus this one, each merge with `rerere` primed by
   the resolutions of this lot with the lots ahead. A conflict that no
   resolution covers ejects the lot (its agent is woken) instead of blocking
   the lots behind it.

Nothing is merged here: `ameesh work land` (L160e2) merges the head of the
queue.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Mapping
from types import SimpleNamespace

from . import packet_guard, plan_git, storage, work
from .canon import FETCH_TIMEOUT, GitError

STATES = ("queued", "landing", "landed", "ejected")
#: pair states where the conflict between two queued lots is not covered
UNPREPARED = ("conflict", "stale", "error", "unclassified")
#: lot branches fetched into the radar's clone for `readiness`
QUEUE_REF = "refs/ameesh/queue/"
#: a milestone names its commit by its id: a ref or a revision expression
#: (`HEAD`, the lot branch) moves, and would admit a commit nobody reviewed
_HEX = re.compile(r"[0-9a-f]{7,64}")


class QueueError(RuntimeError):
    """A lot that cannot enter the queue: actionable message."""


class QueueWithdrawn(QueueError):
    """#3532: the lot was taken out of the queue after the gesture that
    wanted to put it back (`enter(withdrawn_after=…)`)."""


class RegistryReadError(QueueError):
    """#2521 : le registre des agents n'a pas pu être lu.

    Ce n'est pas une famille inconnue (agent absent, harnais non reconnu) :
    conclure `verdicts-missing` sur une lecture impossible ferait écarter un
    lot prêt du paquet. Le diagnostic doit nommer l'échec de lecture."""


# --------------------------------------------------------------------------
# entering and leaving
# --------------------------------------------------------------------------

def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _same(a: str | None, b: str | None) -> bool:
    """Two full commit ids, equal (never a prefix: `readiness` resolves them)."""
    return bool(a and b) and a == b


def pinned(repo: str, sha: str | None) -> str | None:
    """The full id of the commit `sha` names in `repo`, if `sha` is a
    hexadecimal commit id (full or abbreviated); None for a ref, a revision
    expression or an unknown commit."""
    sha = (sha or "").strip().lower()
    if not _HEX.fullmatch(sha):
        return None
    full = plan_git._commit(repo, sha)
    return full if full and full.startswith(sha) else None


def latest_verdict(rows: list[dict]) -> dict | None:
    verdicts = [r for r in rows if r.get("kind") == "verdict"]
    return max(verdicts, key=lambda r: (r.get("at_ts") or 0, int(r.get("id") or 0))) \
        if verdicts else None


# --------------------------------------------------------------------------
# la porte des avis (#1374, P1-1) : comptés, indépendants, sans blocage masqué
# --------------------------------------------------------------------------

#: la classe « sensible » demande un avis d'une autre famille ; **SQL, sécurité
#: et prod** en demandent deux, indépendants (canon §2, décision 18855a). Ces
#: trois domaines se lisent sur les chemins du diff classé (`klass["files"]`).
#: La règle explique leur classe mais peut citer d'autres fichiers inchangés.
#: `critical: true` au canon fait foi ; ces motifs restent un minimum lorsque
#: le canon ne connaît pas encore un chemin critique ou omet sa déclaration.
#: Les motifs propres à ameesh sont ancrés sous `src/ameesh/` et le repli ne
#: s'applique qu'au projet ameesh (#2696) : le crate Nexlink `host-linux`, sous
#: `desktop/`, ne devient plus un chemin de prod par le motif `host[-_]`.
#: Un doute de classe (politique ou diff illisible, `files` vide) n'ajoute
#: aucun de ces domaines : il reste sensible, un avis (canon §2, owner 15/10 :
#: deux familles seulement pour droits SQL, secrets, paiement, prod réelle ;
#: #2394).
CRITICAL_SCOPE = (
    # SQL, migrations, stockage — les noms ameesh sont ancrés sous src/ameesh/ ;
    # `supabase/`, `*.sql` et `deploy/sql/` restent globaux (SQL dans tout projet).
    re.compile(r"^src/ameesh/(?:[^/]+/)*(?:migrations?|storage)(?:/|$)|"
               r"^src/ameesh/(?:[^/]+/)*(?:migrations|db)\.py$|"
               r"(?:^|/)supabase/|\.sql$|^deploy/sql/"),
    # sécurité : droits, identité, clés, secrets, décisions ; garde cgroup
    # DeepSeek (#2282 : le préchargement Node et son module Python). Noms
    # ameesh ancrés sous src/ameesh/, à toute profondeur comme avant #2696
    # (`mediated_executor/identity.py`, `mediated_executor/host_gate.py`).
    re.compile(r"^src/ameesh/(?:[^/]+/)*(?:approve|auth|authority|identity|signing|cose|p256|jcs|"
               r"decisions|visibility|auth_failure|dsn|assignments)(?:/|\.py$|$)|"
               r"^src/ameesh/(?:[^/]+/)*deepseek_cgroup\.(?:cjs|py)$|"
               r"^src/ameesh/(?:[^/]+/)*(?:keys?|secrets?)(?:/|$)|"
               r"^src/ameesh/(?:approve_check|approve_client|config)\.py$"),
    # prod, infra, déploiement, hôtes et relais — dont la porte de fusion.
    # Les chemins ameesh sont ancrés sous src/ameesh/ ; les domaines génériques
    # (deploy, infra, .github, scripts, Dockerfile, pyproject) restent globaux.
    re.compile(r"(?:^|/)(?:deploy|infra|lab|bin)(?:/|$)|(?:^|/)\.github/|"
               r"(?:^|/)scripts/(?:ci|deploy|compute|test)|"
               r"(?:^|/)(?:Dockerfile|pyproject\.toml)$|"
               r"^src/ameesh/(?:[^/]+/)*host[-_]|^src/ameesh/(?:[^/]+/)*(?:hostcheck|exploitation|relay|serve|"
               r"progress_serve|relocation|placement|merge_queue|land|fire)\.py$|"
               # la porte de fusion hors API, la classe du lot et son classement
               # (#2249 ; ligne perdue dans une composition, restaurée #2374)
               r"^src/ameesh/(?:[^/]+/)*(?:merge_pr|lot_class|review\w*|radar|work|train\w*|plan_git|resolutions)\.py$|"
               # ce qui entre dans un paquet : la liste calculée des lots
               # prêts et ses remises (#2496), et la composition scriptée du
               # paquet (#3807 : auto et inclusion entrent dans la porte)
               r"^src/ameesh/packet_candidates\.py$|^scripts/paquet-candidats\.py$|"
               r"^scripts/paquet-(?:auto|inclusion)\.py$|"
               # le report des verdicts acquis sur une tête réalignée (#2442)
               r"^src/ameesh/verdict_carry\.py$|"
               # preuve de famille indépendante des avis (#2265)
               r"^src/ameesh/(?:review_lot|registry)\.py$"),
)
#: la documentation Markdown que le canon classe léger ou normal : les motifs
#: de secours, larges, y verraient un domaine (`docs/design/decisions/`). Seul
#: `critical: true` au canon la rend critique.
_PROSE = re.compile(r"\.(md|markdown)$", re.IGNORECASE)
#: le nombre d'avis d'un lot sensible hors de ces domaines
SENSITIVE_REVIEWS = 1
#: le nombre d'avis d'un lot sensible qui touche SQL, la sécurité ou la prod
CRITICAL_REVIEWS = 2


def _entry_critical(entry) -> bool:
    """Ce chemin classé prouve-t-il SQL, la sécurité ou la prod ?

    `critical: true` du canon fait foi, même porté par une entrée sans chemin.
    Sinon les motifs de secours de `CRITICAL_SCOPE` lisent le chemin des seuls
    **chemins changés**, sauf la documentation Markdown classée légère ou
    normale (le canon peut la classer ainsi).
    """
    if entry.get("critical") is True:
        return True
    path = str(entry.get("path") or "")
    if not path:
        return False
    if (str(entry.get("class") or "") in ("light", "normal")
            and _PROSE.search(path)):
        return False
    return any(rx.search(path) for rx in CRITICAL_SCOPE)


def critical_scope(klass) -> bool:
    """Le diff gelé touche-t-il SQL, la sécurité ou la prod (deux avis) ?

    Le canon peut le déclarer explicitement sur chaque chemin classé. Les
    motifs de secours ameesh sont ancrés sous `src/ameesh/` ; `required_verdicts`
    ne les applique qu'au projet ameesh, la politique compilée des autres
    projets faisant foi (#2696). Ils s'appliquent aussi aux chemins classés
    légers, sauf à la documentation Markdown classée légère ou normale.
    Seuls les **chemins changés** prouvent un domaine critique : une liste
    `files` vide, ou une entrée sans chemin, n'en désigne aucun. Le doute de
    classe n'est pas critique pour autant : `required_verdicts` le garde
    sensible, à un avis (canon §2, owner 15/10 ; #2394). Une déclaration
    `critical: true` du canon reste lue même portée par une entrée sans
    chemin.

    Depuis #3542, `required_verdicts` n'oppose ce plancher qu'aux chemins
    qu'aucune règle `train_reviews` ne nomme ; cette fonction reste la lecture
    globale du diff, pour les autres lecteurs."""
    files = (klass or {}).get("files") or ()
    return any(_entry_critical(entry) for entry in files)


def required_verdicts(klass) -> int:
    """Le nombre d'avis indépendants : deux pour un chemin critique, même si
    le canon l'a omis ou classé léger ; sinon le plancher déclaré pour un lot
    léger (`light_reviews`, §2 : un avis, #2642), 1 pour normal et 1 pour
    sensible hors SQL, sécurité et prod.
    Un lot peut aussi déclarer un nombre d'avis par chemin (`train_reviews`,
    #2696) : le classement le lit et le porte (`train_reviews`), la porte
    l'applique comme la désignation au gel ; un chemin léger déclaré à deux
    avis les exige sans être reclassé, et le doute ne l'efface pas.
    #3542 : le nombre d'une règle qui **nomme** le chemin fait foi, même si le
    chemin correspond à `CRITICAL_SCOPE` ; celui-ci ne relève plus que les
    chemins qu'aucune règle ne nomme, et une règle attrape-tout (`**`) ne
    l'abaisse jamais. `critical: true` du canon reste un plancher explicite,
    pour tous les projets. Sous doute (#2394, dev2 #138909), cette lecture par
    chemin est ignorée — le doute dit que le besoin n'est pas lisible — et
    `CRITICAL_SCOPE` relève le chemin critique non nommé ; le compte global
    déclaré reste un plancher.
    Les motifs de secours `CRITICAL_SCOPE` ne s'appliquent qu'au projet ameesh
    (ou à un appel sans projet connu) : pour les autres projets, la politique
    compilée fait foi (#2696, cas pilote Nexlink `desktop/host/**`) ; une
    déclaration `critical: true` du canon garde deux avis pour tout projet.
    Un lot léger **sans chemin changé** n'a rien à relire : 0. Le plancher est
    lu par le lecteur de classe (`lot_class`), donc le même que la désignation
    au gel et `paquet-candidats` ; il ne reclasse jamais la classe.
    Un doute de classe (politique ou diff illisible, `files` vide) est
    sensible : un avis d'une autre famille (canon §2, owner 15/10 : deux
    familles seulement pour droits SQL, secrets, paiement, prod réelle ;
    #2394). La porte garde l'exigence d'indépendance d'une classe sensible :
    un doute ne se suppose jamais rassurant, mais il n'invente pas un second
    avis que le canon ne demande pas.
    Un lot release n'est jamais léger : il garde un avis (la procédure de
    release et la protection de cible restent au pilote ; le feu, lui, refuse
    la classe release avant tout compte).
    #3183 : le plancher posé par un pilote (`reviews_floor`, lu par la classe)
    relève ce compte, jamais ne l'abaisse ; un lot sans chemin changé reste à
    zéro. Un journal illisible (`reviews_floor_unreadable`) vaut deux avis :
    un plancher qu'on ne lit pas ne se suppose pas absent."""
    base = _scale_verdicts(klass)
    klass = klass or {}
    if base == 0 and not klass.get("files"):
        return 0
    if klass.get("reviews_floor_unreadable"):
        return max(base, CRITICAL_REVIEWS)
    return max(base, int(klass.get("reviews_floor") or 0))


def _named_verdicts(klass, cls: str, project: str) -> int:
    """Le compte par chemin, quand le classement porte la lecture du canon.

    Une règle `train_reviews` qui **nomme** un chemin fixe son nombre : elle
    fait foi contre `CRITICAL_SCOPE` (#3542). Un `critical: true` du canon
    reste un plancher explicite pour tous les projets ; le plancher de classe
    (`light_reviews` compris) s'applique toujours. Une règle `**` couvre tous
    les chemins sans en nommer aucun : elle peut relever le compte (maximum),
    jamais abaisser le plancher critique.
    """
    from . import lot_class
    named = klass.get("train_reviews_paths") or {}
    wide = int(klass.get("train_reviews_wide") or 0)
    floor = (int(klass.get("light_reviews") or 0)
             if cls == lot_class.LIGHT else SENSITIVE_REVIEWS)
    highest = floor
    for entry in klass.get("files") or ():
        path = str(entry.get("path") or "")
        explicit = entry.get("critical") is True
        if not path:
            if explicit:
                highest = max(highest, CRITICAL_REVIEWS)
            continue
        count = named.get(path)
        if count is None:
            value = max(floor, wide)
            if explicit or (project in ("", "ameesh")
                            and _entry_critical(entry)):
                value = max(value, CRITICAL_REVIEWS)
        else:
            value = max(floor, int(count), wide)
            if explicit:
                value = max(value, CRITICAL_REVIEWS)
        highest = max(highest, value)
    return highest


def _scale_verdicts(klass) -> int:
    """Le compte du barème seul (classe et chemins), sans plancher de pilote."""
    from . import lot_class
    klass = klass or {}
    cls = str(klass.get("class") or lot_class.SENSITIVE)
    declared = int(klass.get("train_reviews") or 0)
    project = str(klass.get("project") or "")
    files = klass.get("files") or ()
    if klass.get("doubt"):
        # canon §8 : « en cas de doute, le lot est sensible » — jamais léger,
        # jamais critique par le seul doute (#2394).
        cls = lot_class.SENSITIVE
    # #3542 : un classement qui porte la lecture par chemin fait foi chemin
    # par chemin ; les classements antérieurs (et les appelants qui n'ont que
    # la classe) gardent la lecture globale ci-dessous. Un doute (#2394,
    # dev2 #138909) ne lit jamais ces comptes : ils viennent d'un canon
    # lisible alors que le doute dit que le besoin ne l'est pas ; une règle
    # nommante à 1 y ferait tomber le plancher critique d'un chemin non nommé.
    # Le compte global déclaré reste un plancher dans la lecture de doute.
    if not klass.get("doubt") and "train_reviews_paths" in klass and files:
        return _named_verdicts(klass, cls, project)
    # #2696 : le canon compilé fait foi hors ameesh. Les motifs de secours
    # décrivent les chemins d'ameesh ; sur un lot Nexlink, `host-linux` (un
    # crate sous `desktop/`) ne devient pas un chemin de prod par `host[-_]`.
    # Une déclaration `critical: true` du canon reste lue pour tous les projets.
    explicit_critical = any(entry.get("critical") is True for entry in files)
    fallback = critical_scope(klass) if project in ("", "ameesh") else False
    if files and (explicit_critical or fallback):
        return max(CRITICAL_REVIEWS, declared)
    if cls == lot_class.LIGHT:
        if not klass.get("files"):
            return 0
        return max(int(klass.get("light_reviews") or 0), declared)
    if cls in (lot_class.NORMAL, lot_class.RELEASE):
        return max(1, declared)
    return max(SENSITIVE_REVIEWS, declared)


def _mark_order(mark: dict) -> tuple:
    return (float(mark.get("at_ts") or 0.0), int(mark.get("id") or 0))


def _gate_completed_ts(on_freeze, eligible: dict, *, required: int,
                      waived: bool, distinct_families: bool):
    """#2965 : début de la période ouverte courante, relu chronologiquement.

    La porte ne retient que le **dernier** verdict de chaque acteur ; relire
    son état final ne dit donc pas quand la période ouverte courante a
    commencé. On rejoue ici sa chronologie exacte, du plus ancien au plus
    récent : un blocage courant ferme la porte jusqu'au `ok` du même acteur,
    et l'instant d'admissibilité est celui du verdict qui a **rouvert** la
    porte après la dernière fermeture. Un `ok` redondant sur une porte déjà
    ouverte ne la redate pas.

    Sans avis requis ni blocage, la porte est ouverte dès le gel : la fonction
    rend `None` pour que l'appelant conserve le gel, même si un `ok` arrive
    plus tard. Un blocage, lui, la referme et sa levée date l'ouverture.

    `eligible` associe le nom (casefold) d'un relecteur dont l'avis `ok` est
    retenu par la porte à sa famille ; les avis des auteurs, des correcteurs
    et des familles non prouvées n'y sont pas.
    """
    state: dict = {}
    open_ = bool(waived) or required == 0
    opened_at = None
    for mark in sorted(on_freeze, key=_mark_order):
        actor = str(mark.get("actor") or "").strip().casefold()
        state[actor] = mark
        if any((other.get("verdict") or "") == "blocked" for other in state.values()):
            open_ = False
            continue
        if waived or required == 0:
            complete = True
        else:
            retained = []
            for other in state.values():
                if (other.get("verdict") or "") != "ok":
                    continue
                name = str(other.get("actor") or "").strip().casefold()
                if name not in eligible:
                    continue
                retained.append((_mark_order(other), eligible[name]))
            retained.sort()
            if required >= 2 and distinct_families:
                families = []
                for _order, family in retained:
                    if family not in families:
                        families.append(family)
                complete = len(families) >= required
            else:
                complete = len(retained) >= required
        if not complete:
            open_ = False
            continue
        if not open_:
            open_ = True
            opened_at = float(mark.get("at_ts") or 0.0)
    return opened_at if open_ else None


def _same_sha(a, b, repo: str | None = None) -> bool:
    """Deux identifiants de commit qui nomment le même commit : égaux, ou
    résolus dans `repo` (un verdict peut citer une abréviation)."""
    left, right = str(a or "").strip().lower(), str(b or "").strip().lower()
    if not left or not right:
        return False
    if left == right:
        return True
    if repo:
        full_left, full_right = pinned(repo, left), pinned(repo, right)
        return bool(full_left and full_right and full_left == full_right)
    return False


def actor_family(db, name, *, families=None, declared=False) -> str:
    """La famille de modèle d'un agent, "" si elle n'est pas connue.

    `claude`, `codex`, `deepseek` sont les familles du canon compilé
    (`review_lot.compiled_families`, #2664) ; un humain, un agent absent du
    registre, un harnais non reconnu ou un canon illisible rendent "". Quand
    l'appelant a déjà lu les familles (`families`), elles se réutilisent : la
    porte les lit une fois pour tous les acteurs. L'indépendance d'un avis ne
    se suppose jamais (#1374).

    Une lecture du registre impossible lève `RegistryReadError` (#2521) : la
    famille n'est pas « inconnue », elle n'a pas pu être lue ; les appelants
    qui ne peuvent rien en faire d'autre l'attrapent explicitement. Un canon
    illisible, lui, rend "" : les appelants qui doivent nommer le canon
    (`review_gate`) le lisent avant et échouent fermé avec son diagnostic.

    `declared` (#3578) lit le harnais **déclaré** de l'agent, jamais celui de
    sa session courante : la famille d'un verdict sans reçu ne doit pas suivre
    un repli ultérieur (codex en repli deepseek).
    """
    from . import registry, review_lot
    name = str(name or "").strip()
    if not name or name.startswith(work.HUMAN_PREFIX) or ":" in name:
        return ""
    try:
        row = registry.get(db, name) or {}
    except Exception as exc:  # noqa: BLE001 - #2521 : lecture impossible, pas inconnue
        raise RegistryReadError(
            "cannot read the agent registry for %s: %s" % (name, exc)) from exc
    if families is None:
        try:
            families = review_lot.compiled_families(db)
        except review_lot.FamiliesError:
            return ""
    harness = (str(row.get("harness") or "") if declared
               else registry.live_harness(row))
    return review_lot.family_of(harness, families)


def _actor_family(db, name, families, *, declared=False) -> str:
    """`actor_family` avec les familles déjà lues (ou None : il les lit)."""
    if families is None:
        return actor_family(db, name, declared=declared)
    return actor_family(db, name, families=families, declared=declared)


def _author_families(entry) -> set:
    """Toutes les familles prouvées d'une entrée auteur (#3584).

    `family` porte la famille unique ; `families` porte l'ensemble quand
    l'acteur a écrit sous plusieurs familles. Une entrée sans preuve rend un
    ensemble vide, jamais une famille devinée."""
    from . import review_designation
    return review_designation.proven_families(entry)


def verdict_family(db, mark, *, families=None, receipt=None) -> str:
    """La famille de modèle d'un verdict (#3578), "" si elle n'est pas connue.

    C'est le harnais de son **reçu exact** (#1465/#2928) : la famille sous
    laquelle l'avis a été rendu. Sans reçu (classe légère, verdict antérieur),
    la famille **déclarée** de l'acteur ; jamais le harnais de sa session
    courante — un `ok` rendu sous codex par un agent aujourd'hui en repli
    deepseek reste codex. `receipt` évite de relire un reçu déjà lu.
    Lève `RegistryReadError` comme `actor_family` quand le repli lit le
    registre."""
    from . import review_lot
    actor = str((mark or {}).get("actor") or "").strip()
    if not actor or actor.startswith(work.HUMAN_PREFIX):
        return ""
    if receipt is None:
        receipt = _exact_verdict_receipt(db, mark)
    if receipt is not None:
        if families is None:
            try:
                families = review_lot.compiled_families(db)
            except review_lot.FamiliesError:
                return ""
        return review_lot.family_of(receipt.get("harness"), families)
    return _actor_family(db, actor, families, declared=True)


def blocked_verdicts(marks, frozen, *, repo: str | None = None) -> list[dict]:
    """Les relecteurs dont le **dernier** verdict sur cette tête est `blocked`
    (#1374, P1-1). Un `ok` plus récent du même relecteur lève son blocage ;
    celui d'un autre relecteur ne le masque jamais."""
    latest: dict = {}
    for mark in (marks or ()):
        if mark.get("kind") != "verdict" or not _same_sha(mark.get("sha"), frozen, repo):
            continue
        key = str(mark.get("actor") or "").strip().lower() or "?"
        if key not in latest or _mark_order(mark) > _mark_order(latest[key]):
            latest[key] = mark
    return sorted((m for m in latest.values() if (m.get("verdict") or "") == "blocked"),
                  key=_mark_order)


def is_waived(item) -> bool:
    """La dispense de re-verdict du pilote (`-none`, #1979) sur cette ligne.

    Une seule lecture de la dispense (#2681) : la porte, le gel effectif
    (`plan_git.freeze_candidate`), `paquet-candidats` et le train lisent la
    même marque. Elle couvre la tête gelée, jamais une remise (#2496)."""
    row = item or {}
    return (str(row.get("reviewer") or "").strip() == work.REVIEWER_NONE
            or work.REVIEWER_NONE in tuple(row.get("reviewers") or ()))


def _waiver(db, item) -> tuple:
    """#2281/B1 : la dispense de re-verdict lue **à la porte**, après les jalons.

    `item` peut être une ligne lue avant les jalons : si un gel d'une autre
    tête a été committé entre-temps, le stockage a effacé la marque
    (`_lapse_waiver`) mais cette copie la porte encore. La porte relit donc la
    ligne ici — l'appelant a déjà lu les jalons qu'il nous passe — et une
    dispense périmée ne couvre jamais la nouvelle tête. Une relecture
    impossible ne prouve rien : la dispense est alors tenue pour absente.

    N1 : l'auteur du geste se lit au **journal du lot** (`work.get` ne rend pas
    les événements), pour que la note de file nomme le pilote en usage réel.

    Rend `(waived, waived_by, row)` ; `row` est la ligne fraîche quand elle a
    pu être relue.
    """
    row = item or {}
    lot = row.get("id")
    waived = is_waived(row)
    if waived and lot is not None:
        # Seule une ligne qui *réclame* la dispense peut périmer dans le
        # mauvais sens : relire la base après les jalons ferme la course. Une
        # ligne sans dispense ne peut qu'être plus stricte, rien à relire.
        try:
            fresh = work.get(db, int(lot))
        except Exception:  # noqa: BLE001 - lecture impossible : aucune preuve
            fresh = None
        if fresh is None:
            row = dict(row, reviewer="", reviewers=[])
        else:
            row = fresh
        waived = is_waived(row)
    waived_by = ""
    if waived and lot is not None:
        try:
            events = storage.of(db).work.events_noted(
                "verdict not required", 0.0, ids=[int(lot)])
        except Exception:  # noqa: BLE001 - journal illisible : nom générique
            events = []
        for event in events:
            actor = str(event.get("actor") or "").strip()
            if actor:
                waived_by = actor
    if not waived_by:
        for event in row.get("events") or ():
            if str(event.get("note") or "").startswith("verdict not required"):
                waived_by = str(event.get("actor") or "").strip()
                break
    return waived, waived_by, row


def _attribution_on_freeze(db, marks, frozen, *, repo, klass, item, families=None,
                           commits=None, strict=False, families_out=None,
                           target="", admitted_heads=(), exclusions_out=None):
    """Les auteurs et correcteurs de la tête gelée (#2681/#2739/#2888).

    Lecture des commits exacts de la plage gelée et de leur provenance, la même
    que la désignation : l'auteur de référence, les coauteurs et tout relecteur
    qui a poussé une correction sont des auteurs. Un petit correctif de
    premier tour reste listé comme correcteur (#2739), mais il est **auteur de
    la tête** (#2888) : sa famille est exclue comme celle de tout auteur ; seul
    son nom sert encore à l'exclusion personnelle. Rend `(auteurs,
    correcteurs)`, ou `None` quand aucune lecture n'est possible (pas de base,
    pas de dépôt, pas de ligne de lot) : la porte garde alors son comportement
    historique. Lève quand une lecture était possible et a échoué : une porte
    qui ne peut pas prouver qui a écrit la tête ne compte pas un avis non
    prouvé (l'indépendance ne se suppose pas).

    #2961 : `strict=True` (la porte) refuse un reçu de provenance
    contradictoire au lieu de le lire comme une absence. `families_out` reçoit
    les familles prouvées par acteur : une famille vide est une absence ou une
    ambiguïté, et la porte doit pouvoir les distinguer.

    #2803 : `commits` est la liste déjà calculée du delta propre d'un véhicule
    (`packet_reviews.need`) ; la lecture porte alors sur ce seul contenu relu,
    et `base` n'est plus nécessaire.
    """
    base = str((klass or {}).get("base_sha")
               or (klass or {}).get("target") or "").strip()
    if db is None or not repo or not item:
        return None
    if commits is None and not base:
        return None
    from . import review_authors, review_designation_activation as act
    milestone = _freeze_milestone(marks, frozen, repo)
    # #4791: queue and fire share this bounded, fail-closed attribution.
    # Reuse an outer sweep rather than restarting its budget.
    import contextlib
    outer = review_authors.active_sweep()
    scope = (contextlib.nullcontext(outer) if outer is not None else
             review_authors.sweep_deadline(AUTHORS_SWEEP_BUDGET_S,
                                          "lot #%s" % item.get("id")))
    with scope as sweep:
        try:
            authors, unknown, correctors = act.attribution_of_head(
                db, item, milestone, repo, base, frozen, families=families or (),
                commits=commits, strict=strict, families_out=families_out,
                target=target or base, admitted_heads=tuple(admitted_heads),
                exclusions_out=exclusions_out)
        except review_authors.SweepDeadline as exc:
            raise ValueError("lecture d'auteurs non concluante: %s" % exc) from exc
        if sweep.tripped or sweep.deadline.expired():
            raise ValueError("lecture d'auteurs non concluante: budget exhausted "
                             "at %s" % (sweep.tripped or "attribution"))
    if unknown:
        # #3835 : nommer le gel antérieur et le lot qui portent l'inconnu, et
        # ne proposer `--supersedes` que pour un gel prouvé hors de toute ref.
        raise ValueError(review_authors.unknown_refusal(unknown, head_sha=frozen))
    return authors, correctors


def _corrections_on_freeze(db, marks, frozen, *, repo, klass, item, families=None):
    """Les seuls correcteurs de la tête (#2739), auteurs comme les autres (#2888)."""
    attribution = _attribution_on_freeze(db, marks, frozen, repo=repo, klass=klass,
                                         item=item, families=families)
    if attribution is None:
        return None
    _authors, correctors = attribution
    return {str((row or {}).get("name") or "").strip().lower()
            for row in correctors or () if str((row or {}).get("name") or "").strip()}


def _exact_verdict_receipt(db, mark) -> dict | None:
    """Le reçu exact (#1465) d'un verdict, ou None (#2928).

    En classe sensible, un verdict ne compte que s'il porte son reçu de
    provenance (acteur, session, tour, harnais). Une preuve contradictoire
    (acteur ou tête discordants) n'est pas une preuve : elle est traitée comme
    une absence, jamais comme un avis valable. Sans base, il n'y a rien à
    lire : l'appelant qui passe `family_of` porte alors sa propre preuve
    historique (le train)."""
    if db is None:
        return None
    from . import review_provenance
    try:
        return review_provenance.receipt_for_milestone(db, int((mark or {}).get("id")))
    except (review_provenance.ProvenanceError, KeyError, TypeError, ValueError):
        return None
    except Exception:  # noqa: BLE001 - lecture impossible : aucune preuve
        return None


def _admitted_stacked_parents(db, cfg, item, repo, frozen) -> tuple[str, ...]:
    """Les têtes parentes d'empilement dont la propre porte est satisfaite.

    #4478 : un lot empilé ne relit pas les commits de son parent **relu**.
    Encore faut-il que le parent ait passé sa propre porte sur cette tête
    exacte : les dépendances ne bornent que la prise, pas la fusion, et rien
    n'empêche l'enfant de fusionner avant son parent. Un parent jamais relu,
    ou bloqué, reste donc dans le périmètre de l'enfant : ses auteurs comptent
    (le §2 interdit à leur famille de relire l'enfant), comme pour une tête qui
    n'est pas `admitted_heads` du même véhicule (#3513).

    La porte du parent se lit comme celle des membres d'un paquet : sa classe
    sur sa propre cible, ses avis sur sa tête, et une provenance effectivement
    lue (`attribution_read`, #3513). Une lecture impossible ne l'admet pas :
    le doute conserve les auteurs. Rend les SHA admis, les plus proches
    d'abord (l'ordre de `review_authors.stacked_parents`)."""
    if db is None or cfg is None or not repo or not item:
        return ()
    from . import lot_class, review_authors

    admitted: list[str] = []
    for parent in review_authors.stacked_parents(db, item, repo, frozen):
        lot = int((parent or {}).get("lot") or 0)
        sha = str((parent or {}).get("frozen") or "").strip()
        if not lot or not sha:
            continue
        try:
            row = work.get(db, lot)
            if row is None:
                continue
            marks = storage.of(db).work.milestones(lot, None)
            target = lot_class.default_base(cfg, row, repo)
            base = _class_base(repo, target) if target else None
            base_sha = plan_git._commit(repo, base) if base else None
            klass = lot_class.of_lot(db, lot, cfg=cfg, item=row, repo=repo,
                                     head=sha, milestones=marks,
                                     base=target, base_sha=base_sha)
            if not klass or (klass.get("doubt") and not klass.get("files")):
                continue
            gate = review_gate(db, marks, sha, repo=repo, klass=klass,
                               item=row, cfg=cfg, target=base_sha or target)
        except Exception:  # noqa: BLE001 - porte du parent illisible : non admis
            continue
        if gate.get("ok") and gate.get("attribution_read"):
            admitted.append(sha)
    return tuple(admitted)


def review_gate(db, marks, frozen, *, repo: str | None = None, klass=None,
                item=None, families=None, required=None, family_of=None,
                authors=None, authors_complete=False, cfg=None, project=None,
                target=None, vehicle=None, exclude_author_family=None,
                distinct_families=None, admitted_heads=(), production_cache=None) -> dict:
    """Les avis requis sur la tête gelée `frozen`, et eux seuls (#1374).

    `admitted_heads` ne vient que de la porte du véhicule : les gels de ses
    membres déjà acceptés couvrent leurs commits hérités, dans la lecture
    courante et le report des anciens gels (#3513). La classe reste prudente
    sur le diff complet ; les verdicts restent dus sur la tête exacte.

    Un verdict ne compte que s'il a été rendu **sur cette tête** (le `sha` du
    jalon est résolu, jamais une ref). Parmi ces verdicts, seul le **dernier de
    chaque relecteur** compte : un `ok` plus récent du même relecteur lève son
    blocage, celui d'un autre relecteur ne le masque pas. Un blocage encore en
    vigueur refuse la porte, quelle que soit la classe. Un blocage posé sur une
    **tête antérieure** ne compte pas : la nouvelle tête a ses propres avis
    (canon §2, #2681 ; la règle de levée est celle de la porte).

    Le nombre d'avis dépend de la classe (`required_verdicts`) : le plancher
    déclaré pour un lot léger (#2642), 1 pour un lot normal et pour un lot
    sensible hors SQL/sécurité/prod — un doute de classe reste sensible —,
    2 pour un lot sensible qui touche ces domaines. `required` (#2681) permet
    à un appelant qui lit une politique versionnée plus stricte (le train) de
    passer son compte : la porte garde le **maximum** et ne s'affaiblit jamais.
    Les avis comptés sont **indépendants** : jamais le travail d'un auteur ;
    pour un lot léger à plancher déclaré et pour une classe sensible, une
    famille de modèle connue et autre que celle de tout auteur ; pour deux
    avis, deux familles distinctes. L'indépendance ne se suppose jamais : une
    famille d'auteur inconnue refuse la porte (canon §2, décision 18855a).

    `authors` (#2681) est l'ensemble des auteurs **reconnus** de la tête, tel
    que l'appelant l'a lu (`review_authors.authors`) : `{name, family}`. La
    porte ne compare plus seulement à l'assigné : un coauteur — tiers avec
    reçu exact —, tout relecteur qui a poussé une correction, même petite
    (#2888), et les auteurs d'un gel antérieur dont le contenu subsiste (#2924)
    ne relisent ni eux-mêmes ni leur famille. La famille de l'assigné
    historique reste celle de la trace du gel (`family_of`) : le reçu d'un
    autre commit du même nom ne la remplace pas. Sans `authors`, la porte garde
    son comportement historique : seul l'assigné est connu.

    #2808 : l'assigné **titulaire** n'est auteur que s'il a écrit un commit de
    la plage gelée. Quand la porte lit la plage, son nom absent des auteurs
    lus le laisse hors des auteurs ; `authors_complete=True` dit la même chose
    d'une liste lue par l'appelant (le train lit la plage exacte et refuse un
    auteur inconnu). Sans cette preuve, la porte garde la référence — une
    liste partielle ne prouve pas l'absence d'écriture, et l'indépendance ne se
    suppose jamais. Un titulaire sans commit ne ferme donc plus sa famille ;
    s'il a écrit, sa famille reste exclue avec la famille de son gel.

    `family_of` (#2681) est la source historique de famille du train : le même
    code de porte, mais les familles des reçus/tours à la date des faits au
    lieu du registre courant. Un appelant qui le fournit ne lit ni le registre
    ni le canon compilé.

    #2961 : sans `family_of`, la famille de la référence se lit sur la même
    trace du gel que la désignation (`review_authors.reference_author`) : reçu
    du commit, journal des tours, sinon reçu du jalon de gel. Le registre
    courant ne remplace pas une famille historique prouvée — un auteur gelé en
    codex puis rouvert en claude au registre garde codex (#2892) ; un reçu de
    provenance contradictoire refuse la porte (lecture `strict`).

    #2726 : sans `authors`, la porte lit elle-même les auteurs reconnus de la
    plage gelée quand le dépôt, la base et la ligne du lot sont connus
    (`attribution_of_head`) — le feu, la file et le paquet excluent donc tous
    les auteurs, pas seulement l'assigné ; une plage illisible échoue fermé.
    `exclude_author_family` et `distinct_families` viennent de l'appelant ou
    sinon de `klass` (drapeaux compilés du canon) : le premier rend la règle
    stricte même à un avis, le second décide de la double famille au compte 2.

    #2803 : quand le véhicule a un delta propre, la porte lit les auteurs et
    les correcteurs sur **les commits de ce delta** (`vehicle["commits"]`),
    pas sur toute la plage gelée. Les familles des lots membres, déjà relues
    sur leur propre tête, ne sont donc pas exclues une seconde fois, et le
    composeur n'est pas une référence d'office : il ne compte que s'il a
    écrit un commit de ce delta. Le commit de manifeste est hors delta
    (`packet_reviews._is_review_free`) ; la lecture le filtre encore par
    sûreté. Sans delta propre, aucun avis n'est dû (`required == 0`).

    #2739/#2888 : le relecteur qui a poussé un correctif est un auteur de la
    tête. Son avis est écarté **par son nom** (la lecture exacte de la plage,
    `corrections_of_head`) et sa famille est exclue comme celle de tout auteur.
    Quand la plage ne peut pas être lue, la porte échoue fermé si la lecture
    était possible (dépôt, base et ligne de lot connus).

    #2664 : les familles viennent du canon compilé (`families`, sinon lues par
    `review_lot.compiled_families` au dernier commit audité). Un canon
    illisible refuse la porte **fermée** avec le canon nommé — une seule
    synchro ratée ne bloque pas tant qu'un commit audité reste lisible.

    #1979 : le pilote peut marquer « verdict non requis » (`work.reviewer
    --no-verdict`, colonne `reviewer = -none`, geste d'autorité journalisé) ;
    la marque lève alors l'exigence d'avis **de cette tête gelée** — le
    stockage la périme à un nouveau gel (`_lapse_waiver`). #2281 : la marque
    est relue à la porte, **après** les jalons, pour qu'une ligne lue avant un
    gel ne couvre jamais la nouvelle tête. Un verdict bloquant courant, lui,
    n'est jamais levé par la dispense : la correction reste due.

    #2766 : pour un **véhicule de paquet** (`integration/**`), la porte ne
    classe pas le diff gelé entier. `packet_reviews.need` lit les avis de
    chaque membre sur SA tête gelée et ne retient que le delta propre du
    véhicule (résolution non mécanique, adaptation) : sans delta, aucun avis
    de véhicule n'est dû ; un membre sans avis refuse. `vehicle` permet à un
    appelant qui a déjà fait cette lecture de la passer ; sinon elle est faite
    ici quand `cfg` et `target` sont fournis. Un véhicule sans membre lisible
    garde la lecture historique, jamais un passage à zéro avis.

    #2928 : en classe sensible, un verdict sans reçu exact de provenance
    (#1465) ne compte pas — même s'il nomme la bonne tête. Le filtre précède
    le « dernier de chaque relecteur » : un faux `ok` ne masque plus un vrai
    `blocked`, et sa famille ne peut plus faire passer la porte. Les verdicts
    écartés sont rendus dans `unproven`.

    #2965 : `completed_ts` publie le début de la période ouverte courante,
    # relu chronologiquement (dernier verdict de chaque acteur, blocage levé
    # par le `ok` du même acteur) : un `ok` redondant ne redate pas une porte
    # déjà ouverte, un blocage la referme et sa levée date la réouverture. Sans
    # blocage ni avis requis, il vaut None (la porte est ouverte au gel). C'est
    # l'instant d'admissibilité du train, pas une nouvelle règle pour la file.

    #3400 : sur le chemin anticipé (#3397), les `ok` comptés ne sont pas des
    # avis **acceptés** — le filtre d'indépendance (auteurs, correcteurs,
    # familles) n'a pas été appliqué, et la lecture qui le prouverait est
    # précisément celle qu'on évite. Ils sont donc rendus dans
    # `accepted_unfiltered` ; `accepted` reste la liste des seuls avis dont
    # l'indépendance est prouvée, vide quand la porte se ferme sans la lire.

    Rend `{ok, why, class, required, accepted, accepted_unfiltered, blocked,
    waived, author, author_family, author_families, authors, corrections,
    on_freeze, unproven, completed_ts, vehicle, attribution_read}` ; ne lève
    jamais. `author_families` porte toutes les familles prouvées exclues
    (#3584) ; `author_family` reste la famille unique, vide si plusieurs.
    `attribution_read` prouve une lecture effective de la provenance ; une
    dispense ou une porte sans avis requis ne fournit pas cette preuve.
    """
    from . import lot_class
    klass = klass or {}
    cls = str(klass.get("class") or lot_class.SENSITIVE)
    frozen = str(frozen or "").strip().lower()
    if klass.get("doubt"):
        cls = lot_class.SENSITIVE
    # #2726 : les drapeaux compilés du canon (`reviews`) sont portés par la
    # classe lue (`lot_class`), ou passés explicitement par le train depuis son
    # document. Absents (canon antérieur), on garde le comportement historique :
    # pas d'exclusion de famille d'auteur, deux familles distinctes au compte 2.
    if exclude_author_family is None:
        exclude_author_family = bool(klass.get("exclude_author_family", False))
    if distinct_families is None:
        distinct_families = bool(klass.get("distinct_families", True))
    # #1979/#2281 : la dispense de re-verdict du pilote (`-none`) lève la
    # porte, comme elle lève l'attente dans `progress` ; elle ne survit pas à
    # un nouveau gel (stockage `_lapse_waiver`) et ne lève jamais un blocage.
    # La marque est relue après les jalons (`_waiver`) pour ne pas appliquer
    # une copie antérieure au gel.
    waived, waived_by, row = _waiver(db, item)
    author = str(row.get("assignee") or "").strip()
    out = {"ok": False, "why": "", "error": "", "class": cls, "required": 0,
           "accepted": [], "accepted_unfiltered": [], "blocked": [],
           "rejected": [],
           "waived": waived, "waived_by": waived_by,
           "author": author, "author_family": "", "authors": [],
           "corrections": [], "on_freeze": 0, "unproven": [], "vehicle": None,
           "vehicle_why": "",
           "attribution_read": False, "provenance_exclusions": [],
           "provenance_manifest": "",
           "exclude_author_family": exclude_author_family,
           "distinct_families": distinct_families}
    if not frozen:
        out["why"] = "no freeze to review: the fire names the exact frozen head"
        return out
    if vehicle is None and cfg is not None and target and item is not None:
        from . import packet_reviews
        if packet_reviews.is_vehicle(item):
            try:
                vehicle = packet_reviews.need(db, int(item.get("id") or 0), item,
                                              marks, repo or "", target, frozen,
                                              cfg=cfg)
            except Exception as exc:  # noqa: BLE001 - lecture impossible : refus
                vehicle = {"ok": False, "required": None, "klass": None,
                           "why": "vehicle review unreadable: %s" % exc,
                           "members": [], "own_paths": []}
    if vehicle is not None:
        out["vehicle"] = vehicle
        if vehicle.get("ok") is False:
            out["why"] = (vehicle.get("why")
                          or "no review verdict ok on the freeze %s (class: %s): "
                             "the vehicle's constituents or proper delta are not "
                             "proven" % (frozen[:12], cls))
            return out
        if vehicle.get("klass"):
            # #2766 (B cx3) : la classe du delta propre vient de
            # `class_of_paths` et ne porte ni `base_sha` ni `target`. La base
            # de provenance reste celle de la plage gelée du lot : sans elle,
            # `_corrections_on_freeze` rend None et le correcteur d'un petit
            # correctif compterait sur son propre delta (#2739/#2888).
            outer = klass or {}
            provenance = {key: outer[key] for key in ("base_sha", "target")
                          if outer.get(key)}
            klass = dict(vehicle["klass"], **provenance)
            cls = str(klass.get("class") or cls)
            if klass.get("doubt"):
                cls = lot_class.SENSITIVE
            # #4041 : la porte lit la classe du delta propre, pas celle du
            # diff gelé entier ; la trace le dit.
            out["vehicle_why"] = "proper delta %s (%s)" % (
                cls, klass.get("why") or ", ".join(vehicle.get("own_paths") or ())
                or "?")
    vehicle_required = 0
    if vehicle is not None and vehicle.get("ok") is not False:
        vehicle_required = int(vehicle.get("required") or 0)
        if vehicle.get("ok") is True and vehicle_required == 0 and cls != lot_class.RELEASE:
            klass = {"class": lot_class.LIGHT, "files": []}
            cls = lot_class.LIGHT
            # #4041 : sans cette trace, `queue add` affichait « class light »
            # suivi de la raison sensible du diff gelé entier.
            out["vehicle_why"] = (vehicle.get("why")
                                  or "constituents reviewed on their frozen heads")
    # #2803 : le seul contenu que la porte du véhicule relit est son delta
    # propre (`commits`) ; les membres ont leurs avis sur leur propre tête.
    own_commits = [str(sha).strip() for sha in (vehicle or {}).get("commits") or ()
                   if str(sha).strip()]
    vehicle_delta = (vehicle is not None and vehicle.get("ok") is None
                     and bool(own_commits))
    floor = required_verdicts(klass)
    if required is None:
        required = floor
    elif type(required) is not int or required not in (0, 1, 2):
        out["error"] = "explicit review requirement expected"
        out["why"] = ("no review verdict ok on the freeze %s (class: %s): an explicit"
                      " review requirement of 0, 1 or 2 is required, not %r"
                      % (frozen[:12], cls, required))
        return out
    required = max(required, floor, vehicle_required)
    out["required"] = required
    out["class"] = cls
    on_freeze = [m for m in (marks or ()) if m.get("kind") == "verdict"
                 and _same_sha(m.get("sha"), frozen, repo)]
    out["on_freeze"] = len(on_freeze)
    # #2928 : en classe sensible, seul un verdict qui porte son reçu exact
    # (#1465) compte. Sans cette preuve, un verdict fabriqué au nom d'un
    # relecteur pendant son tour était compté par le journal, et pouvait
    # masquer son vrai dernier avis. Le filtre précède le « dernier de chaque
    # relecteur » : un faux ok ne masque plus un vrai block, et un faux block
    # ne bloque plus une tête.
    unproven: list[dict] = []
    receipts: dict = {}
    if cls == lot_class.SENSITIVE and db is not None:
        proven: list = []
        for mark in on_freeze:
            actor = str(mark.get("actor") or "").strip()
            if actor.startswith(work.HUMAN_PREFIX):
                # la règle vise un **verdict d'agent** sans reçu : un humain
                # n'a pas de tour managé (sa signature est un autre contrôle).
                proven.append(mark)
                continue
            receipt = _exact_verdict_receipt(db, mark)
            if receipt is None:
                unproven.append({"id": mark.get("id"),
                                 "actor": mark.get("actor") or "",
                                 "verdict": mark.get("verdict") or "",
                                 "at_ts": mark.get("at_ts")})
            else:
                # #3578 : le reçu lu ici donne aussi la famille du verdict.
                receipts[id(mark)] = receipt
                proven.append(mark)
        on_freeze = proven
    out["unproven"] = unproven
    latest: dict = {}
    for mark in on_freeze:
        key = str(mark.get("actor") or "").strip().lower() or "?"
        if key not in latest or _mark_order(mark) > _mark_order(latest[key]):
            latest[key] = mark
    blocked = sorted((m for m in latest.values() if (m.get("verdict") or "") == "blocked"),
                     key=_mark_order)
    out["blocked"] = [{"actor": m.get("actor") or "", "at_ts": m.get("at_ts"),
                       "note": m.get("note") or ""} for m in blocked]
    if blocked:
        out["why"] = ("no review verdict ok on the freeze %s (class: %s): blocked verdict(s) "
                      "not lifted: %s"
                      % (frozen[:12], cls, ", ".join(sorted(
                          str(m.get("actor") or "relecteur sans nom") for m in blocked))))
        return out
    if waived or required == 0:
        # #2965 : sans avis requis, la porte est ouverte dès le gel ; un
        # blocage courant la referme jusqu'à sa levée, et c'est alors le `ok`
        # qui la rouvre qui date l'admissibilité, jamais le gel (B2).
        out["completed_ts"] = _gate_completed_ts(
            on_freeze, {}, required=required, waived=waived,
            distinct_families=distinct_families)
        out["ok"] = True
        return out
    if family_of is None and families is None and db is not None:
        from . import review_lot
        try:
            families = review_lot.compiled_families(db)
        except review_lot.FamiliesError as exc:
            # #2664 : canon illisible -> échec fermé, avec le canon nommé. Un
            # commit audité restant lisible suffit ; seule son absence refuse.
            out["error"] = str(exc)
            out["why"] = ("no review verdict ok on the freeze %s (class: %s): the model "
                          "families cannot be read (%s), so no verdict can be proven "
                          "independent (canon §2: independence is never assumed)"
                          % (frozen[:12], cls, exc))
            return out

    def _family_of(name):
        if family_of is not None:
            value = family_of(name)
            return "" if value is None else str(value)
        return _actor_family(db, name, families)

    def _verdict_family(mark):
        # #3578 : la famille d'un verdict est celle de son reçu (harnais au
        # moment de l'avis), à défaut la famille déclarée de l'acteur — jamais
        # le harnais de sa session courante. Le train (`family_of`) porte déjà
        # sa lecture historique des verdicts (`verdict_families`).
        if family_of is not None:
            return _family_of(mark.get("actor"))
        return verdict_family(db, mark, families=families,
                              receipt=receipts.get(id(mark)))

    provided: dict[str, str] = {}
    provided_families: dict[str, set] = {}
    known_authors: dict[str, dict] = {}
    for entry in authors or ():
        if isinstance(entry, Mapping):
            name = str(entry.get("name") or "").strip()
            family = str(entry.get("family") or "").strip()
            proven = _author_families(entry)
        else:
            name, family, proven = str(entry or "").strip(), "", set()
        if not name:
            continue
        if family:
            provided.setdefault(name.lower(), family)
            proven = set(proven) | {family.strip().lower()}
        if proven:
            provided_families.setdefault(name.lower(), set()).update(proven)
        # #3968 : la lecture d'auteurs déjà faite par l'appelant (la file, le
        # feu) permet au refus anticipé de nommer les auteurs et leurs familles
        # au lieu d'affirmer « no author reading ».
        known_authors[name.lower()] = {
            "name": name,
            "families": sorted(value for value in proven if value)}
    # #3397 : la porte se ferme sans lire la plage gelée quand les `ok` de la
    # tête sont déjà trop peu nombreux pour atteindre le compte requis, quels
    # que soient leurs auteurs. `_attribution_on_freeze` lit Git sur chaque
    # fichier de la plage (des dizaines de secondes sur un gros lot sensible)
    # et la passe d'alertes (`_review_gate_open`) l'appelait pour rendre ce
    # refus. Compter les `ok` par acteur suffit : la lecture des auteurs ne
    # peut qu'en écarter, jamais en ajouter. Sans connexion (banc, porte
    # appelée sans base), la lecture est de toute façon impossible : le chemin
    # historique garde son message d'échec fermé.
    latest_ok = [mark for mark in latest.values()
                 if (mark.get("verdict") or "") == "ok"]
    if len(latest_ok) < required and callable(getattr(db, "query", None)):
        # #3400 (auditeur sur #3397) : ces `ok` ne sont pas des avis
        # **acceptés**. Le filtre d'indépendance (auteurs, correcteurs,
        # familles) n'a pas été appliqué — c'est la lecture Git qu'on évite —
        # donc `accepted` les compterait à tort. Ils restent lisibles sous
        # `accepted_unfiltered`, un nom qui ne les fait pas passer pour des
        # avis indépendants ; aucune décision ne lit `accepted` sur une porte
        # fermée (`packet_candidates` exige `ok`).
        unfiltered = [{"actor": str(mark.get("actor") or ""),
                       "family": _verdict_family(mark),
                       "id": mark.get("id"), "at_ts": mark.get("at_ts")}
                      for mark in sorted(latest_ok, key=_mark_order)]
        out["accepted_unfiltered"] = unfiltered
        need = ("two independent verdicts from different model families, neither from "
                "an author's" if required >= 2 else "one verdict from another agent")
        got = ", ".join("%s (%s)" % (entry["actor"] or "?",
                                     entry["family"] or "famille inconnue")
                        for entry in unfiltered) or "aucun"
        out["why"] = ("no review verdict ok on the freeze %s (class: %s): %d independent "
                      "review verdict(s) required, %s; got %s"
                      % (frozen[:12], cls, required, need, got))
        # #3397 : le retour anticipé garde le diagnostic du chemin historique
        # (#2928) : les verdicts écartés faute de reçu exact sont déjà lus
        # ci-dessus, sans Git ; sans ce suffixe, la porte refusée pour un
        # verdict sensible sans reçu ne disait plus pourquoi il ne comptait pas.
        if unproven:
            out["why"] += ("; %d verdict(s) without an exact provenance receipt were "
                           "not counted (sensitive class, #2928)" % len(unproven))
        # #3968 : quand l'appelant a déjà lu les auteurs (la file les passe pour
        # que chaque avis soit prouvé indépendant), le refus les nomme. Dire
        # « no author reading » alors que la lecture existe faisait chercher un
        # défaut d'attribution : sur #3882 la famille codex venait du siège
        # manuel codex1, pas d'un auteur, et le refus ne le disait pas.
        if authors is not None:
            rows = []
            declared = set()
            for key in sorted(known_authors):
                entry = known_authors[key]
                entry_families = list(entry["families"])
                declared.update(entry_families)
                row = {"name": entry["name"],
                       "family": entry_families[0] if len(entry_families) == 1 else ""}
                if len(entry_families) > 1:
                    row["families"] = entry_families
                rows.append(row)
            out["authors"] = rows
            out["author_families"] = sorted(declared)
            if rows:
                reading = ", ".join(
                    "%s (%s)" % (row["name"],
                                 ", ".join(row["families"]) if row.get("families")
                                 else row["family"] or "family unknown")
                    for row in rows)
                out["why"] += ("; author reading of this head: %s; the missing ok "
                               "verdict(s) are not on this head (a verdict on another "
                               "head does not count)" % reading)
            else:
                out["why"] += ("; the supplied author reading names no author of this "
                               "head and no other ok verdict names it")
        elif unfiltered:
            out["why"] += ("; these %d ok verdict(s) are not proven independent "
                           "(no author reading)" % len(unfiltered))
        return out
    # #2726 : la porte lit elle-même la plage gelée quand l'appelant ne passe
    # pas les auteurs — le feu, la file et le paquet, qui ne les lisaient pas.
    # La même lecture rend les correcteurs (#2739) et échoue fermé quand la
    # provenance manque : une porte qui ne peut pas prouver qui a écrit la tête
    # ne compte pas un avis non prouvé (l'indépendance ne se suppose pas).
    try:
        from . import provenance_scope
        if production_cache is None:
            production_cache = (vehicle or {}).pop("production_cache", {})
        cache_key = str(repo) + "\n" + str((item or {}).get("app") or "")
        if cache_key not in production_cache:
            try:
                production_cache[cache_key] = (provenance_scope.production(cfg, item, repo), "")
            except Exception as exc:  # noqa: BLE001 - failure grants no exclusion
                production_cache[cache_key] = (None, str(exc))
        production, publication_error = production_cache[cache_key]
        if publication_error:
            out["production_scope_error"] = publication_error
        inherited = tuple((vehicle or {}).get("admitted_heads") or ())
        from . import review_authors
        # #4478 : une tête de parent d'empilement n'est hors du périmètre de
        # relecture de l'enfant que si sa **propre** porte est satisfaite sur
        # cette tête exacte, comme les `admitted_heads` des membres d'un même
        # véhicule (#3513). Un parent jamais relu ou bloqué reste compté :
        # l'enfant ne peut pas le faire atterrir sans une relecture
        # indépendante de sa famille d'auteur.
        stacked = tuple(
            {"head": sha, "reason": "stacked-parent", "ref": "parent"}
            for sha in _admitted_stacked_parents(db, cfg, item, repo, frozen))
        scopes = (tuple(admitted_heads) + inherited + stacked
                  + ((production,) if production else ()))
        if scopes and repo:
            boundary = (review_authors.target_commit(
                            repo, target or (klass or {}).get("target") or "",
                            fetch=True)
                        or (klass or {}).get("base_sha"))
            if boundary:
                listing = plan_git._out(repo, "rev-list", "%s..%s" % (boundary, frozen))
                if listing is None:
                    raise ValueError("provenance exclusion range unreadable")
                provenance_scope.exclude(repo, listing.split(), scopes,
                                         out["provenance_exclusions"])
        out["provenance_manifest"] = provenance_scope.manifest(out["provenance_exclusions"])
    except Exception as exc:  # unreadable publication never authorizes exclusions
        out["error"] = str(exc)
        out["why"] = "provenance scope unreadable: %s" % exc
        return out
    corrections: set = set()
    stacked_doubts: dict = {}
    proven_families: dict = {}
    try:
        attribution = _attribution_on_freeze(db, marks, frozen, repo=repo,
                                             klass=klass, item=item,
                                             families=families,
                                             commits=own_commits if vehicle_delta else None,
                                             strict=True,
                                             families_out=proven_families,
                                             target=target or "",
                                             admitted_heads=scopes,
                                             exclusions_out=out["provenance_exclusions"])
        # #3544 : le report des gels antérieurs complète la trace.
        out["provenance_manifest"] = provenance_scope.manifest(out["provenance_exclusions"])
    except Exception as exc:  # noqa: BLE001 - une plage illisible ne conclut pas
        out["error"] = str(exc)
        out["why"] = ("no review verdict ok on the freeze %s (class: %s): the frozen "
                      "range cannot be read (%s), so an author's own verdict and a "
                      "reviewer's own correction cannot be excluded (canon §2: "
                      "independence is never assumed)" % (frozen[:12], cls, exc))
        return out
    # #2803 : la lecture a établi les auteurs du seul delta relu. Sans elle
    # (pas de dépôt ou de ligne), la porte garde la référence historique.
    out["attribution_read"] = attribution is not None
    exact = vehicle_delta and attribution is not None
    range_authors: list = []
    if attribution is not None:
        range_authors, range_correctors = attribution
        if authors is None or exact:
            authors = range_authors
        corrections = {str((row or {}).get("name") or "").strip().lower()
                       for row in range_correctors or ()
                       if str((row or {}).get("name") or "").strip()}
        # #5066 : l'exclusion par un commit d'un lot empilé est un doute, à
        # dire comme tel.
        stacked_doubts = {str(row["name"]).strip().lower(): row
                          for row in range_correctors or ()
                          if (row or {}).get("stacked_lot") and row.get("name")}
        # #5055 : ascendance illisible dans ce clone : la porte reste fermée
        # et dit quel lot empilé elle n'a pas pu vérifier.
        unread_stacks = {str(row["name"]).strip().lower(): int(row["stacked_unread"])
                         for row in range_correctors or ()
                         if (row or {}).get("stacked_unread") and row.get("name")}
    out["corrections"] = sorted(corrections)
    # #2961 : la famille de la référence est celle de la **trace du gel**,
    # lue avec la plage gelée (`review_authors.reading` : reçu exact du commit,
    # journal des tours du commit, sinon famille de référence du gel par
    # `reference_author` en `strict=True` ci-dessus). Le registre courant ne
    # remplace plus une famille historique prouvée : un auteur gelé en codex
    # puis rouvert en claude au registre garde codex comme référence (#2892).
    # `proven_families` distingue une famille vide par absence de preuve d'une
    # famille vide par ambiguïté (B1) : seule l'absence garde le repli
    # historique ; l'ensemble prouvé est exclu famille par famille (#3584).
    reference_entry = next(
        (row for row in range_authors or ()
         if isinstance(row, Mapping) and author
         and str(row.get("name") or "").strip().lower() == author.lower()),
        None)

    def _author_names(rows) -> set:
        return {str((row or {}).get("name") or "").strip().lower()
                if isinstance(row, Mapping) else str(row or "").strip().lower()
                for row in rows or ()}

    # #2808 : le titulaire n'est auteur que s'il a écrit un commit de la plage.
    # La lecture de la plage (ou une liste que l'appelant déclare complète et
    # exacte, le train) dit s'il a écrit ; sans aucune preuve, il reste la
    # référence (direction stricte : l'indépendance ne se suppose jamais).
    if authors_complete and not exact and authors is not None:
        # La lecture complète inclut les auteurs de fusion ; la lecture des
        # seuls commits de contenu ne peut pas effacer cette preuve (#1515).
        reference_is_author = bool(author) and author.lower() in _author_names(authors)
    elif attribution is not None:
        reference_is_author = bool(author) and author.lower() in _author_names(
            range_authors)
    else:
        reference_is_author = True
    # #2961 B1 / #3584 : une famille vide n'est pas toujours une absence de
    # preuve. `families_out` porte les familles réellement prouvées ; >1 pour
    # la référence (journal du gel codex + reçu de commit claude, par exemple)
    # n'est pas une ambiguïté à refuser : chacune de ses familles est exclue,
    # et aucune famille unique n'est devinée au registre courant (#2755).
    # L'absence de preuve garde, elle, le repli historique.
    reference_proven: set = set()
    if author and reference_is_author:
        if attribution is not None:
            reference_proven = set(proven_families.get(author.lower()) or ())
        reference_proven |= set(provided_families.get(author.lower()) or ())
    try:
        if family_of is not None:
            author_family = _family_of(author)
            if not author_family and author.lower() in provided:
                author_family = provided[author.lower()]
        elif reference_is_author and reference_proven:
            author_family = (next(iter(reference_proven))
                             if len(reference_proven) == 1 else "")
        elif reference_entry is not None and not exact:
            author_family = str(reference_entry.get("family") or "")
            if not author_family:
                author_family = (provided.get(author.lower(), "")
                                 or _family_of(author))
        elif author.lower() in provided:
            author_family = provided[author.lower()]
        else:
            author_family = _family_of(author)
    except RegistryReadError as exc:
        # #2521 : le registre est illisible, ce n'est pas « famille inconnue ».
        # La porte refuse toujours (l'indépendance ne se suppose pas) mais le
        # diagnostic doit dire la lecture impossible, pas un verdict manquant.
        out["error"] = str(exc)
        out["why"] = ("no review verdict ok on the freeze %s (class: %s): the agent "
                      "registry cannot be read (%s), so no verdict can be proven "
                      "independent (canon §2: independence is never assumed)"
                      % (frozen[:12], cls, exc))
        return out
    out["author_family"] = author_family
    # #3584 : un auteur lu sans famille unique mais avec un ensemble prouvé
    # n'est pas refusé ici : `recognized` porte l'ensemble et la porte exclut
    # chacune de ses familles. Une entrée sans aucune preuve reste refusée
    # plus bas (famille inconnue, indépendance non prouvée).
    # #2681/#2808/#2888 : les auteurs **reconnus** de la tête, quand l'appelant
    # les a lus (`review_authors.authors`). Un tiers avec reçu exact et tout
    # relecteur qui a poussé un correctif, même petit, s'y ajoutent : leur
    # famille est exclue.
    recognized: dict = {}
    if author and not exact and reference_is_author:
        row = {"name": author, "family": author_family}
        if len(reference_proven) > 1:
            row["families"] = sorted(reference_proven)
        recognized[author.lower()] = row
    for entry in authors or ():
        if isinstance(entry, Mapping):
            name = str(entry.get("name") or "").strip()
            family = str(entry.get("family") or "").strip()
            proven = _author_families(entry)
        else:
            name, family, proven = str(entry or "").strip(), "", set()
        if not name:
            continue
        key = name.lower()
        if author and key == author.lower() and not exact:
            # La référence est déjà reconnue, avec la famille de la trace du
            # gel (`family_of`) : le reçu d'un autre commit du même nom ne
            # prouve pas sa famille au gel. Quand la lecture a établi qu'elle
            # n'a rien écrit (#2808), elle ne se réintroduit pas par une liste
            # d'appelant. Sur un véhicule dont le delta est lu (#2803),
            # l'assigné n'est pas une référence : seul un reçu de commit le
            # nomme auteur.
            continue
        if family:
            proven = set(proven) | {family.strip().lower()}
        # #3658 : défense en profondeur — une entrée sans `families` garde
        # l'ensemble que la lecture de la plage a prouvé pour ce nom, avant
        # tout repli sur la seule famille du registre.
        proven = set(proven) | set(proven_families.get(key) or ())
        if not proven:
            # Absence de preuve (aucun ensemble) : le repli historique du
            # registre reste, comme avant #3584. Une famille inconnue y est
            # refusée par le contrôle strict plus bas.
            try:
                family = _family_of(name)
            except RegistryReadError as exc:
                out["error"] = str(exc)
                out["why"] = ("no review verdict ok on the freeze %s (class: %s): the agent "
                              "registry cannot be read (%s), so the author %s cannot be "
                              "proven independent (canon §2: independence is never assumed)"
                              % (frozen[:12], cls, exc, name))
                return out
            if family:
                proven = {family.strip().lower()}
        known = recognized.get(key)
        if known is None:
            row = {"name": name, "family": ""}
            if len(proven) == 1:
                row["family"] = next(iter(proven))
            elif len(proven) > 1:
                row["families"] = sorted(proven)
            recognized[key] = row
        else:
            merged = _author_families(known) | proven
            known.pop("families", None)
            if len(merged) > 1:
                known["family"] = ""
                known["families"] = sorted(merged)
            elif len(merged) == 1:
                known["family"] = next(iter(merged))
    out["authors"] = sorted(recognized.values(), key=lambda entry: entry["name"].lower())
    # L'indépendance ne se suppose jamais (canon §2, décision 18855a) : une
    # classe sensible — y compris un doute de classe, qui peut cacher SQL, la
    # sécurité ou la prod — et un lot léger à plancher déclaré (#2642 : le §2
    # demande « un avis d'une autre famille ») n'acceptent que des familles
    # connues et autres que celles de **tous** les auteurs. Famille d'auteur
    # inconnue (agent absent du registre, harnais non reconnu) : aucun avis ne
    # peut être prouvé indépendant, la porte refuse et le dit. Un auteur humain
    # n'a pas de famille de modèle : il n'y a rien à comparer, seules les
    # familles des relecteurs comptent.
    # #2726 : `exclude_author_family` du canon rend la règle stricte même à un
    # avis sur une classe normale ; `distinct_families` décide de la double
    # famille au compte 2 (la porte garde le comportement historique sans
    # drapeau lu).
    strict = (required >= 2 or cls in (lot_class.LIGHT, lot_class.SENSITIVE)
              or exclude_author_family)
    if strict and not recognized:
        # Aucun auteur connu (assigné absent, ni `authors`) : l'indépendance
        # ne se suppose pas, comme avant #2681.
        out["why"] = ("no review verdict ok on the freeze %s (class: %s): the author %s has "
                      "no known model family, so no verdict can be proven independent "
                      "(canon §2: independence is never assumed)"
                      % (frozen[:12], cls, author or "(none)"))
        return out
    if strict:
        for entry in recognized.values():
            # #3584 : un ensemble prouvé (même de plusieurs familles) est une
            # preuve ; seule une entrée sans aucune famille reste inconnue.
            if (_author_families(entry)
                    or entry["name"].lower().startswith(work.HUMAN_PREFIX)):
                continue
            out["why"] = ("no review verdict ok on the freeze %s (class: %s): the author %s "
                          "has no known model family, so no verdict can be proven "
                          "independent (canon §2: independence is never assumed)"
                          % (frozen[:12], cls, entry["name"]))
            return out
    author_families: set = set()
    for entry in recognized.values():
        author_families.update(_author_families(entry))
    out["author_families"] = sorted(author_families)
    accepted = []
    rejected: list[dict] = []
    for mark in sorted((m for m in latest.values() if (m.get("verdict") or "") == "ok"),
                       key=_mark_order):
        actor = str(mark.get("actor") or "").strip()
        if actor and actor.lower() in recognized:
            # #3968 : le refus nommera cet avis et sa cause, au lieu de laisser
            # le lecteur deviner pourquoi une famille « interdite » l'est.
            raised = recognized.get(actor.lower()) or {}
            raised_families = sorted(_author_families(raised))
            rejected.append({"actor": actor,
                             "family": (raised_families[0]
                                        if len(raised_families) == 1 else ""),
                             "families": raised_families, "reason": "author"})
            continue                      # on ne relit pas son propre travail
        if actor and actor.lower() in corrections:
            # #5197 : le refus dit la famille du relecteur (celle de son avis),
            # pas « famille inconnue » ; l'exclusion, elle, est par le nom.
            try:
                shown = _verdict_family(mark) or ""
            except RegistryReadError:
                shown = ""
            rejected.append({"actor": actor, "family": shown, "reason": "corrector"})
            doubt = stacked_doubts.get(actor.lower())
            if doubt:
                rejected[-1].update(reason="stacked-corrector",
                                    stacked_lot=int(doubt["stacked_lot"]),
                                    rewritten=str(doubt.get("rewritten") or ""))
            elif unread_stacks.get(actor.lower()):
                rejected[-1]["stacked_unread"] = unread_stacks[actor.lower()]
            continue                      # l'auteur d'un correctif ne le relit pas (#2739)
        try:
            family = _verdict_family(mark)
        except RegistryReadError as exc:
            out["error"] = str(exc)
            out["why"] = ("no review verdict ok on the freeze %s (class: %s): the agent "
                          "registry cannot be read (%s), so the reviewer %s cannot be "
                          "proven independent (canon §2: independence is never assumed)"
                          % (frozen[:12], cls, exc, actor or "(none)"))
            return out
        if strict:
            if not family:
                rejected.append({"actor": actor, "family": "",
                                 "reason": "unknown-family"})
                continue                  # famille du relecteur inconnue : non prouvée
            if family in author_families:
                rejected.append({"actor": actor, "family": family,
                                 "reason": "author-family"})
                continue                  # la famille d'un auteur ne compte pas
        accepted.append({"actor": actor, "family": family, "id": mark.get("id"),
                         "at_ts": mark.get("at_ts")})
    out["rejected"] = rejected
    # #2965 : la période ouverte courante se lit sur **tous** les avis
    # retenus, pas seulement sur les `required` premiers choisis pour la
    # famille : un avis d'une troisième famille peut être celui qui a rouvert
    # la porte après un blocage, et la troncature ci-dessous l'écarterait. On
    # calcule donc avant la troncature.
    out["completed_ts"] = _gate_completed_ts(
        on_freeze,
        {str(entry["actor"] or "").strip().casefold(): entry["family"]
         for entry in accepted},
        required=required, waived=False, distinct_families=distinct_families)
    if required >= 2 and distinct_families:
        chosen, seen = [], set()
        for entry in accepted:
            if entry["family"] in seen:
                continue
            seen.add(entry["family"])
            chosen.append(entry)
            if len(chosen) >= required:
                break
        accepted = chosen
    out["accepted"] = accepted
    if len(accepted) >= required:
        out["ok"] = True
        return out
    got = ", ".join("%s (%s)" % (e["actor"] or "?", e["family"] or "famille inconnue")
                    for e in accepted) or "aucun"
    need = ("two independent verdicts from different model families, neither from an author's"
            if required >= 2 else
            "one verdict from another family than the author's" if strict
            else "one verdict from another agent")
    out["why"] = ("no review verdict ok on the freeze %s (class: %s): %d independent review "
                  "verdict(s) required, %s; got %s"
                  % (frozen[:12], cls, required, need, got))
    # #3968 : le refus nomme les familles d'auteurs de la tête et chaque avis
    # écarté avec sa cause. Une famille « interdite » sans cette preuve se
    # lisait comme un défaut d'attribution (nexlink #3882 : la famille codex
    # venait d'un siège manuel, pas d'un commit de la plage).
    if author_families:
        out["why"] += ("; author families of this head: %s"
                       % ", ".join(sorted(author_families)))
    if rejected:
        causes = {"author": "author of the head",
                  "corrector": "reviewer who corrected the head",
                  "stacked-corrector": "doubt: reviewer's commit on a lot stacked"
                                       " on this freeze may have been carried here;"
                                       " another reviewer of the family can review",
                  "unknown-family": "unknown model family (independence not proven)",
                  "author-family": "family of an author of the head"}
        out["why"] += "; not counted: %s" % ", ".join(
            "%s (%s): %s" % (row["actor"] or "?", row["family"] or "family unknown",
                             causes.get(row["reason"], row["reason"]))
            for row in rejected)
    if unproven:
        out["why"] += ("; %d verdict(s) without an exact provenance receipt were not "
                       "counted (sensitive class, #2928)" % len(unproven))
    return out


#: #3761 : la porte des avis se relit à chaque passe d'alertes. Sa lecture
#: Git (survie du contenu des correctifs, auteurs de la plage gelée) coûtait
#: 4 à 5 min de CPU par passe et re-prouvait les mêmes faits tant que le gel,
#: la cible et les jalons ne bougeaient pas. Le cache ne garde que le verdict
#: booléen de la porte, indexé par les entrées dont il dépend ; une entrée
#: périmée (TTL) ou dont la signature change est relue.
#:
#: Le cache ne nourrit que la passe d'alertes (`exploitation._slo_breaches`) :
#: aucun chemin de fusion ne le lit. Une fusion refait toujours la porte
#: complète ; un verdict mémorisé ne peut donc jamais autoriser un merge.
GATE_CACHE_VERSION = "3761.2"
#: Une famille d'acteur ou un reçu de provenance peut changer sans nouveau
#: jalon : la porte se relit au plus tard après ce délai.
GATE_CACHE_TTL_S = 3600.0
#: Le document partagé reste petit : au-delà, les plus anciennes entrées
#: partent (la porte se relit alors au prochain passage).
GATE_CACHE_MAX_ENTRIES = 2000

#: Les modules qui décident du verdict de porte. Leur contenu entre dans la
#: signature : une règle changée par un lot (#3762 par exemple) ne doit jamais
#: servir un verdict calculé par le code d'avant, même si les jalons et le gel
#: sont identiques. C'est une empreinte du code réellement exécuté, pas le
#: numéro de paquet (qui ne bouge qu'aux releases).
_GATE_CODE_FILES = (
    "merge_queue.py", "review_authors.py", "review_designation_activation.py",
    "lot_class.py", "plan_git.py", "modules/dev/plan_git.py", "canon.py",
    "review_lot.py", "review.py", "provenance_scope.py", "packet_reviews.py",
    "modules/dev/lot_class.py", "modules/dev/review.py", "modules/dev/review_target.py",
)
_GATE_CODE_FINGERPRINT: str | None = None


def _gate_code_digest(root: str) -> str:
    """L'empreinte du code de la porte sous `root` (le dossier `ameesh`).

    Les chemins déplacés par une tranche de module (L100) comptent par leur
    nouveau fichier, pas seulement par l'alias constant laissé derrière :
    `plan_git.py` n'est qu'un alias depuis #4383, le vrai code est dans
    `modules/dev/plan_git.py`. Un chemin absent est haché comme
    `unreadable` : son apparition change l'empreinte."""
    digest = hashlib.sha256()
    for name in _GATE_CODE_FILES:
        digest.update(name.encode("utf-8"))
        try:
            with open(os.path.join(root, name), "rb") as fh:
                digest.update(fh.read())
        except OSError:
            digest.update(b"unreadable")
    return digest.hexdigest()[:16]


def _gate_code_version() -> str:
    """La version du code de la porte : numéro de paquet + empreinte source.

    L'empreinte est calculée une fois par processus. Un arbre de test lancé par
    `PYTHONPATH=src` et une installation déployée ont donc chacun la leur."""
    global _GATE_CODE_FINGERPRINT
    if _GATE_CODE_FINGERPRINT is not None:
        return _GATE_CODE_FINGERPRINT
    root = os.path.dirname(os.path.abspath(__file__))
    try:
        from . import version as ameesh_version
        label = ameesh_version()
    except Exception:  # noqa: BLE001 - version illisible : l'empreinte suffit
        label = "unknown"
    _GATE_CODE_FINGERPRINT = "%s+%s" % (label, _gate_code_digest(root))
    return _GATE_CODE_FINGERPRINT


def _gate_cache_signature(cfg, item, marks, repo, frozen, target, base,
                          base_sha, klass=None) -> str:
    """Les entrées dont le verdict de porte dépend, sans lecture Git.

    Les jalons sont figés par identifiant et contenu (une annulation ou un
    verdict ajouté change la signature) ; la cible et sa base entrent dans la
    clé, car un commit déjà fusionné ne compte plus comme auteur (#3116). Le
    canon et sa référence décident de la classe et des familles compilées ;
    la version du code de la porte périme tout résultat d'avant une règle."""
    item = dict(item or {})
    mark_sig = sorted(
        (str(mark.get("id") or ""), str(mark.get("kind") or ""),
         str(mark.get("sha") or ""), str(mark.get("actor") or ""),
         str(mark.get("verdict") or ""), str(mark.get("at_ts") or ""),
         bool(mark.get("annulled")))
        for mark in marks or () if isinstance(mark, Mapping))
    return GATE_CACHE_VERSION + ":" + hashlib.sha256(json.dumps([
        GATE_CACHE_VERSION,
        _gate_code_version(),
        str(item.get("id") or ""), str(item.get("app") or ""),
        str(item.get("assignee") or ""), str(item.get("package_id") or ""),
        str(item.get("branch_target") or ""), str(item.get("type") or ""),
        str(repo or ""), str(frozen or ""), str(target or ""),
        str(base or ""), str(base_sha or ""),
        str(getattr(cfg, "canon", "") or ""),
        str(getattr(cfg, "canon_ref", "") or ""),
        str((klass or {}).get("class") or ""),
        mark_sig,
    ], sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _gate_cache_store(cache, lot, signature, ok, now: float) -> None:
    """Retient un verdict concluant ; une lecture illisible ne se cache pas."""
    if not isinstance(cache, dict) or not isinstance(ok, bool):
        return
    cache[str(lot)] = {"key": signature, "ok": bool(ok),
                       "at": round(float(now), 3)}
    if len(cache) > GATE_CACHE_MAX_ENTRIES:
        oldest = sorted(cache, key=lambda key: float(
            (cache.get(key) or {}).get("at") or 0.0))
        for key in oldest[:len(cache) - GATE_CACHE_MAX_ENTRIES]:
            cache.pop(key, None)


def review_gate_for_item(db, cfg, item: dict, marks, *, klass=None,
                         overview=None, gate_cache=None,
                         now: float | None = None) -> dict | None:
    """La porte des avis du dernier gel d'un lot, lue comme la composition du
    paquet (`packet_candidates`) : dépôt retrouvé par le commit gelé (#2559,
    l'hôte qui lit le lot n'est pas toujours celui de son auteur), gel résolu
    en commit complet puis classe du diff gelé, puis `review_gate`.

    `overview` : les lignes de `registry.overview` déjà lues par l'appelant
    (une passe SLO sur N lots = une lecture, #2564) ; `None` les relit.

    `gate_cache` (#3761) : dictionnaire de verdicts déjà lus, porté par l'état
    de la passe d'alertes. Une signature identique et fraîche rend le verdict
    sans relire Git ; une entrée absente, périmée ou de signature différente
    relit la porte et remplace l'entrée. `None` garde la lecture complète.
    Seule `exploitation._slo_breaches` passe un cache : le feu, la file et le
    paquet l'appellent sans, donc une fusion ne lit jamais un verdict mémorisé.

    Rend None quand la lecture n'est pas concluante (pas de gel, dépôt
    introuvable, classe en doute sans chemin lisible) : l'appelant ne doit
    jamais lire None comme une porte ouverte. Lecture seule."""
    from . import lot_class

    now = time.time() if now is None else float(now)
    freeze = plan_git.latest_freeze(marks or ())
    frozen = str((freeze or {}).get("sha") or "").strip()
    repo, resolved = (lot_class.repo_for_freeze(db, cfg, item, frozen,
                                                overview=overview)
                      if frozen else ("", ""))
    if resolved:
        # #2564 : la classe et la porte lisent le commit complet, jamais le
        # préfixe saisi dans le jalon de gel.
        frozen = resolved
    try:
        # Comme la file : origin/<cible> avant la branche locale, qui peut
        # encore contenir dans le diff des changements déjà fusionnés (#2207).
        target = lot_class.default_base(cfg, item, repo) if repo else None
        base = _class_base(repo, target) if target else None
        base_sha = plan_git._commit(repo, base) if base else None
    except Exception:  # noqa: BLE001 - une lecture ratée ne conclut pas
        return None
    lot = int((item or {}).get("id") or 0)
    signature = _gate_cache_signature(cfg, item, marks, repo, frozen, target,
                                      base, base_sha, klass) \
        if gate_cache is not None else None
    if gate_cache is not None:
        entry = gate_cache.get(str(lot))
        if isinstance(entry, dict) and isinstance(entry.get("ok"), bool) \
                and entry.get("key") == signature:
            try:
                age = now - float(entry.get("at"))
            except (TypeError, ValueError):
                age = None
            if age is not None and 0.0 <= age <= GATE_CACHE_TTL_S:
                return {"ok": bool(entry.get("ok")), "cached": True}
    # #4469 : sous une échéance de balayage (passe d'alertes), une lecture
    # dont un balayage a été refusé ne conclut pas : None, et rien en cache.
    from . import review_authors
    sweep = review_authors.active_sweep()
    trips = sweep.trips if sweep is not None else 0
    try:
        klass = klass or lot_class.of_lot(
            db, lot, cfg=cfg, item=item,
            repo=repo or None, head=frozen or None, milestones=marks or None,
            base=base, base_sha=base_sha)
    except Exception:  # noqa: BLE001 - une lecture ratée ne conclut pas
        return None
    if not klass:
        return None
    if klass.get("doubt") and not klass.get("files"):
        return None
    frozen = str(klass.get("frozen_sha") or frozen or "").strip()
    if not frozen:
        return None
    try:
        gate = review_gate(db, marks or [], frozen,
                           repo=klass.get("repo") or repo or None,
                           klass=klass, item=item, cfg=cfg, target=target)
    except Exception:  # noqa: BLE001 - une porte illisible ne conclut pas
        return None
    if sweep is not None and sweep.trips != trips:
        return None
    if gate_cache is not None and isinstance(gate, dict):
        _gate_cache_store(gate_cache, lot, signature, gate.get("ok"), now)
    return gate


def _remote_branch(name: str) -> str:
    """The branch of `origin` that a lot branch or a target names:
    `origin/main`, `refs/remotes/origin/main` and `main` all name `main`."""
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _class_base(repo: str, target: str, *, queue: bool = False) -> str | None:
    """La référence de cible qui explique la classe d'un lot, ou None.

    #2207 : un `main` local en retard sur `origin/main` faisait entrer dans le
    diff des commits déjà fusionnés (dont des chemins sensibles) et classait
    `sensitive` un lot léger. La cible distante dit ce que la file va
    fusionner : `origin/<cible>` avant la cible locale dans un checkout.

    Dans le clone de la file (`queue=True`), **seule** la cible récupérée
    fraîchement sous `QUEUE_REF` fait foi : `origin/<cible>` y est la copie que
    le clone `--shared` a prise du checkout des agents, donc la cible locale
    périmée de sa source, jamais l'origin du projet (P2-1 de la revue R1)."""
    wanted = _remote_branch(target)
    candidates = ([QUEUE_REF + wanted] if queue
                  else ["origin/" + wanted, wanted])
    for candidate in candidates:
        if plan_git._commit(repo, candidate):
            return candidate
    return None


def _class_ref(repo: str, target: str, sha: str, *, queue: bool = False) -> str:
    """La référence de `repo` qui nomme la cible fraîche `sha`, sinon le commit.

    Le diff de classe se lit toujours entre **les mêmes** deux commits : une
    source dont la référence de cible ne pointe pas sur `sha` est périmée et
    ne doit pas fournir une autre borne (P2-2 de la revue R1)."""
    wanted = _remote_branch(target)
    candidates = ([QUEUE_REF + wanted] if queue
                  else ["origin/" + wanted, wanted])
    for candidate in candidates:
        if plan_git._commit(repo, candidate) == sha:
            return candidate
    return sha


class _ProvenanceError(RuntimeError):
    """Les auteurs complets d'un lot ne peuvent pas être établis (#1515)."""


def _delta_base(repo: str, row, target: str | None = None, *,
                other: str | None = None, queue: bool = False) -> tuple[str, str, str]:
    """(commit, ref, dépôt) de la base effective du delta gelé (#1515 B2).

    La ref privée de la file (`refs/ameesh/queue/<branche>`) n'est lue que
    dans le clone de la file (`queue=True`) : c'est le radar qui l'y laisse
    fraîche. Dans le checkout d'un agent, c'est une ref **locale** que l'agent
    peut déplacer (`git update-ref`) jusqu'à vider le delta et se réattribuer
    les commits ; elle n'y prouve rien (R6 B2). Une cible non qualifiée
    (`develop`) a été contrôlée par le feu sous `origin/develop` : la ref
    **distante** passe avant la branche locale, qui peut porter un gel non
    publié et masquer le correcteur ; une ref explicitement qualifiée est
    gardée. Une cible introuvable lève : la porte ne lit jamais un delta vide
    faute de borne (réécriture #1515 de la préférence de cible de `04e1d85e`,
    sans en recopier l'arbre)."""
    target = str(target or (row or {}).get("branch_target") or "").strip()
    if not target:
        try:
            target = plan_git.default_target(repo) or ""
        except (plan_git.MergeProbeError, GitError) as exc:
            raise _ProvenanceError("no effective target for the frozen delta: %s"
                                   % exc) from exc
    if not target:
        raise _ProvenanceError("no effective target for the frozen delta (branch_target, "
                               "project target and repository default are all empty)")
    short = _remote_branch(target)
    places = [repo] + ([other] if other and other != repo else [])
    # #4673 : la ref distante du checkout n'est fraîche que de son dernier
    # fetch. Le delta de la porte se lit sur la **pointe publiée** de la
    # cible : sans cela, les commits que la cible a gagnés depuis comptent
    # comme des tiers de la plage et refusent la provenance. La lecture est
    # best-effort : une forge muette laisse la ref locale (doute fermé).
    if not queue:
        try:
            from . import review_authors
            fresh = review_authors.refreshed_target(repo, target)
        except Exception:  # noqa: BLE001 - rafraîchissement au mieux
            fresh = ""
        if fresh:
            return fresh, target, repo
    private = QUEUE_REF + short
    if queue:
        for place in places:
            try:
                found = plan_git._commit(place, private)
            except GitError as exc:
                raise _ProvenanceError("target %s is unreadable in %s (%s)"
                                       % (private, place, exc)) from exc
            if found:
                return found, private, place
    refs = [target]
    if target == short:
        refs.insert(0, "refs/remotes/origin/" + short)
    else:
        refs += ["origin/" + short, short]
    for ref in dict.fromkeys(refs):
        if not ref:
            continue
        for place in places:
            try:
                found = plan_git._commit(place, ref)
            except GitError as exc:
                raise _ProvenanceError("target %s is unreadable in %s (%s)"
                                       % (ref, place, exc)) from exc
            if found:
                return found, ref, place
    raise _ProvenanceError("target %s is not readable in %s"
                           % (target, " or ".join(places)))


def _delta_head(repo, frozen, *, target_repo: str | None = None) -> tuple[str, str]:
    """(commit complet, dépôt) de la tête gelée (#1515).

    La tête peut vivre dans le dépôt du lot ou dans celui de la cible (objets
    non partagés d'un banc ou d'un clone incomplet). Une tête introuvable
    lève : la porte ne lit jamais un delta vide faute de tête."""
    places = [repo] + ([target_repo] if target_repo and target_repo != repo else [])
    for place in places:
        try:
            head = plan_git._commit(place, str(frozen))
        except GitError as exc:
            raise _ProvenanceError("the repository %s is unreadable (%s)"
                                   % (place, exc)) from exc
        if head:
            return head, place
    raise _ProvenanceError("the frozen head %s is not readable in %s"
                           % (str(frozen)[:12], " or ".join(places)))


def _delta_log(repo, row, frozen, *, base: str | None = None,
               base_repo: str | None = None, target: str | None = None,
               target_repo: str | None = None, queue: bool = False,
               args=(), exclude=()) -> list[str]:
    """Les lignes de `git log <args> base..head` (#1515 N1/B3).

    Le delta est lu depuis la **base effective** (`_delta_base`, refs privées
    de la file comprises). `args` choisit la lecture : noms Git des auteurs,
    commits de contenu (`--no-merges`) ou auteurs des fusions (`--merges`).
    Une fusion peut ajouter du contenu : son auteur est un auteur du lot et ne
    peut plus compter comme relecteur indépendant. `exclude` nomme des têtes
    déjà relues ailleurs (parents d'empilement, #4478) : leur histoire sort du
    delta, fusions comprises. Lève `_ProvenanceError` quand le gel, la base ou
    `git log` ne peut pas être lu : la porte refuse, elle n'invente pas une
    liste partielle."""
    if not repo or not frozen:
        return []
    head, head_repo = _delta_head(repo, frozen, target_repo=target_repo)
    if not base or not base_repo:
        base, _base_ref, base_repo = _delta_base(repo, row, target,
                                                 other=target_repo, queue=queue)
    if base == head:
        return []
    argv = list(args) + ["%s..%s" % (base, head)]
    excluded = [str(sha).strip() for sha in exclude or () if str(sha).strip()]
    if excluded:
        argv += ["--not", *excluded]
    proc = plan_git._git(head_repo, "log", *argv)
    if proc.returncode != 0 and base_repo != head_repo:
        # Objets non partagés (banc, clone incomplet) : la tête peut aussi
        # vivre dans le dépôt de la cible.
        try:
            has_head = plan_git._commit(base_repo, str(frozen))
        except GitError as exc:
            raise _ProvenanceError("the repository %s is unreadable (%s)"
                                   % (base_repo, exc)) from exc
        if has_head:
            proc = plan_git._git(base_repo, "log", *argv)
    if proc.returncode != 0:
        detail = (proc.stderr.decode("utf-8", "replace").strip()
                  or "code %d" % proc.returncode)
        raise _ProvenanceError("the delta %s..%s is unreadable (%s)"
                               % (base[:12], head[:12], detail))
    return [line.strip() for line in
            proc.stdout.decode("utf-8", "replace").splitlines() if line.strip()]


def _unique_names(names) -> list[str]:
    """Les noms non vides, sans doublon de casse, dans l'ordre lu."""
    out: list[str] = []
    for name in names:
        name = str(name or "").strip()
        if name and name.lower() not in {value.lower() for value in out}:
            out.append(name)
    return out


def _delta_commits(repo, row, frozen, *, base: str | None = None,
                   base_repo: str | None = None, target: str | None = None,
                   target_repo: str | None = None, queue: bool = False,
                   exclude=()) -> list[str]:
    """Les commits de contenu du delta gelé (#1515), fusions exclues.

    C'est la plage que lit le lecteur partagé (`review_authors.reading`) et la
    désignation : une fusion n'est pas un commit de contenu, son auteur se lit
    à part (`_merge_authors`) pour ne pas perdre une résolution."""
    return _unique_names(_delta_log(
        repo, row, frozen, base=base, base_repo=base_repo, target=target,
        target_repo=target_repo, queue=queue, exclude=exclude,
        args=("--no-merges", "--format=%H")))


def _merge_authors(repo, row, frozen, *, base: str | None = None,
                   base_repo: str | None = None, target: str | None = None,
                   target_repo: str | None = None, queue: bool = False,
                   exclude=()) -> list[str]:
    """Les auteurs Git des fusions du delta gelé (#1515 N1/B3)."""
    return _unique_names(_delta_log(
        repo, row, frozen, base=base, base_repo=base_repo, target=target,
        target_repo=target_repo, queue=queue, exclude=exclude,
        args=("--merges", "--format=%an")))


def _commit_author(places, sha: str) -> str:
    """Le nom Git d'un commit que le lecteur partagé n'a pas su attribuer.

    Le lecteur partagé reste la source des auteurs reconnus ; ce repli ne sert
    qu'à ne pas oublier un commit sans preuve lisible (outil, humain, reçu
    absent). Il rend "" quand aucun dépôt ne peut lire le commit."""
    for place in places:
        text = plan_git._out(place, "show", "-s", "--format=%an", sha)
        if text is not None:
            return text.strip()
    return ""


def _object_recoverer(where: str, sources):
    """Rapatrie par SHA un commit historique absent du dépôt de lecture (#2924 B7).

    Le clone scratch de `merge-pr` ne contient que la cible et la tête ; les
    commits d'un gel antérieur réécrit n'y sont pas. On les demande au dépôt de
    l'agent ou à l'origine du projet, puis on vérifie que l'objet est bien
    lisible localement. Aucun ref n'est déplacé et aucune tête n'est changée :
    seule la preuve manquante est réparée. Rend False (jamais une exception)
    quand la récupération échoue, pour que l'appelant strict refuse fermé.

    #4457 (B/P2 cx3) : `timeout` est le temps qui reste à l'appelant ; il
    borne l'ensemble des sources, pas chacune. Sans lui, le plafond par source
    reste `FETCH_TIMEOUT` (comportement historique)."""
    def recover(sha: str, *, timeout=None) -> bool:
        text = str(sha or "").strip().lower()
        if not _HEX.fullmatch(text):
            return False
        try:
            if plan_git.local_commit(where, text):
                return True
        except Exception:  # noqa: BLE001 - dépôt illisible : on tente les sources
            pass
        # Un objet non complet ne se demande pas par SHA ; il a déjà été résolu
        # localement s'il était lisible.
        if len(text) not in (40, 64):
            return False
        remaining = None if timeout is None else max(float(timeout), 0.0)
        for source in sources or ():
            source = str(source or "").strip()
            if not source:
                continue
            if remaining is not None and remaining <= 0:
                break
            bound = (FETCH_TIMEOUT if remaining is None
                     else min(remaining, FETCH_TIMEOUT))
            started = time.monotonic()
            try:
                proc = plan_git._git(where, "fetch", "--quiet", "--no-tags",
                                     "--no-write-fetch-head", "--", source, text,
                                     timeout=bound)
            except Exception:  # noqa: BLE001 - source injoignable : suivante
                proc = None
            if remaining is not None:
                remaining -= time.monotonic() - started
            if proc is None or proc.returncode != 0:
                continue
            try:
                if plan_git.local_commit(where, text):
                    return True
            except Exception:  # noqa: BLE001 - lecture impossible : source suivante
                continue
        return False
    return recover


def _freeze_milestone(marks, frozen, repo: str | None = None):
    """Le jalon de gel quand il nomme cette tête, forme du lecteur partagé.

    Le lecteur (`review_authors.reading`) et la désignation attendent
    `{id, sha, actor, at_ts}` ; un gel qui nomme une autre tête ne désigne
    personne sur celle-ci (#2739)."""
    freeze = plan_git.latest_freeze(marks or ())
    if freeze is None or not _same_sha(freeze.get("sha"), frozen, repo):
        return None
    return {"id": int(freeze.get("id") or 0),
            "sha": str(freeze.get("sha") or ""),
            "actor": freeze.get("actor") or "",
            "at_ts": freeze.get("at_ts")}


def _complete_authors(db, item, frozen, repo: str | None, target: str | None,
                      *, target_repo: str | None = None, queue: bool = False,
                      klass=None, marks=None, cfg=None):
    """(auteurs `{name, family}`, familles) pour la porte des avis (#1515 N1).

    `cfg` est nécessaire pour admettre les têtes d'un parent d'empilement
    (#4478) : seule une tête dont la propre porte est satisfaite sort de la
    lecture ; sans `cfg`, aucune ne l'est (le doute garde les auteurs).

    Les auteurs de **contenu** sont ceux du lecteur partagé (#2681,
    `review_authors.reading`) : reçus exacts, familles **historiques** du
    harnais qui a réellement tourné, coauteurs et correcteurs de relecteur,
    quelle que soit la taille de leur correction (#2888). S'y ajoutent les
    auteurs des gels antérieurs du lot dont le contenu subsiste (#2924) : une
    réécriture d'historique à contenu identique ne rend pas une famille
    indépendante. Un commit que le lecteur n'attribue pas garde son auteur Git,
    relu au registre courant : la porte n'oublie pas un auteur parce que sa
    preuve manque, et ne suppose jamais une famille. S'y ajoutent les
    **auteurs des fusions** : une fusion peut ajouter du contenu et ne peut pas
    compter comme relecteur indépendant.

    #2808 : le titulaire n'est **pas semé** dans les auteurs par sa seule
    qualité d'assigné ; les détenteurs du journal non plus. Un détenteur
    n'entre que par un commit de la plage gelée (auteur Git, reçu exact ou
    repli #2721), exactement comme le lit `review_authors.reading`. La
    **référence** (l'assigné au gel, `reference_author`) garde son repli de
    famille pour attribuer les commits sans reçu ; elle ne devient pas un
    auteur pour autant.

    #2924 B7 : le clone scratch de `merge-pr` ne contient que la cible et la
    tête. Un gel antérieur réécrit y est absent ; la porte tente de le
    rapatrier par SHA depuis le dépôt de l'agent puis l'origine du projet
    (`recover`), et ne refuse que si cette récupération échoue. Un acteur
    prouvé sous plusieurs familles (`families_out`, #3584) rend tout l'ensemble
    dans `families` : chacune de ses familles est exclue, jamais une famille
    unique devinée.

    Un auteur hors registre (outil, humain non préfixé) n'a pas de famille de
    modèle à comparer : il est omis de la liste, jamais supposé. Un correctif
    de relecteur, même petit (#2739), fait de son auteur un auteur de la tête :
    sa famille est exclue, et la porte écarte aussi son nom de son propre
    correctif (#2888). Sans magasin (`db is None`, banc ou dry-run), la
    provenance n'est pas lisible : les auteurs Git du delta restent la source,
    comme la porte historique. Toute lecture illisible — lecteur partagé,
    cible, gel, `git log`, registre — lève `_ProvenanceError` : la porte
    refuse au lieu d'accepter une liste partielle."""
    from . import resolutions, review_authors, review_lot
    if not repo:
        raise _ProvenanceError("no repository for the frozen delta")
    try:
        families = review_lot.compiled_families(db)
    except review_lot.FamiliesError as exc:
        raise _ProvenanceError("the model families cannot be read (%s)" % exc) from exc
    base, _base_ref, base_repo = _delta_base(repo, item, target,
                                             other=target_repo, queue=queue)
    places = [repo]
    for place in (target_repo, base_repo):
        if place and place not in places:
            places.append(place)
    # #2924 B7 : les objets d'un gel antérieur réécrit ne sont pas dans le
    # clone scratch de `merge-pr`. On tente de les rapatrier par SHA depuis le
    # dépôt de l'agent puis l'origine du projet, avant tout refus.
    sources: list[str] = []
    for place in (target_repo, repo, base_repo):
        text = str(place or "").strip()
        if text and text != repo and text not in sources:
            sources.append(text)
    for place in (target_repo, repo, base_repo):
        text = str(place or "").strip()
        if not text:
            continue
        try:
            url = resolutions._local_from(  # #3041 : relatif à `text`
                text, plan_git._out(text, "remote", "get-url", "origin") or "")
        except Exception:  # noqa: BLE001 - remote illisible : pas de source
            url = ""
        if url and url not in sources:
            sources.append(url)
    recover = _object_recoverer(repo, sources)
    reference_name = ""
    reader_names: set[str] = set()
    proven_families: dict[str, set] = {}
    found: dict[str, dict] = {}

    def add(name, family="", families=()) -> None:
        name = str(name or "").strip()
        if not name or name.lower().startswith(work.HUMAN_PREFIX) or ":" in name:
            return
        key = name.lower()
        row = found.setdefault(key, {"name": name, "families": set()})
        values = [family]
        if isinstance(families, (list, tuple, set, frozenset)):
            values.extend(families)
        for value in values:
            value = str(value or "").strip().lower()
            if value:
                row["families"].add(value)

    parent_heads: tuple = ()
    if db is None:
        # Sans magasin (banc, dry-run), la provenance n'est pas lisible : on
        # garde la lecture Git historique, comme `_corrections_on_freeze`
        # s'abstient déjà sans magasin. La production fournit toujours `db`.
        for name in _unique_names(_delta_log(
                repo, item, frozen, base=base, base_repo=base_repo, target=target,
                target_repo=target_repo, queue=queue, args=("--format=%an",))):
            add(name)
    else:
        # #4478 : un lot empilé sur la tête gelée d'un parent (prérequis
        # déclaré ou membre du paquet) ne relit pas les commits de ce parent
        # **s'il a passé sa propre porte**. Un parent jamais relu ou bloqué
        # reste dans le delta : sa famille d'auteur compte, et la porte ne peut
        # pas lui opposer un avis de cette famille. La borne s'applique aussi
        # aux fusions du parent.
        parent_heads = _admitted_stacked_parents(db, cfg, item, repo, frozen)
        commits = _delta_commits(repo, item, frozen, base=base, base_repo=base_repo,
                                 target=target, target_repo=target_repo, queue=queue,
                                 exclude=parent_heads)
        milestone = _freeze_milestone(marks, frozen, repo)
        reference_families: dict = {}
        try:
            # #2808 : la référence est l'assigné **au gel**, pas le titulaire
            # courant ; elle n'est auteur que par un commit de la plage. Le
            # repli de famille ne vaut que pour attribuer un commit sans reçu.
            # #2961 : ses familles prouvées passent à la lecture pour que deux
            # preuves différentes (journal du gel + reçu) restent ambiguës.
            reference = review_authors.reference_author(
                db, item, milestone, families, strict=True,
                families_out=reference_families)
        except Exception as exc:  # noqa: BLE001 - référence illisible : refus
            raise _ProvenanceError(
                "the reference author of the frozen head cannot be read (%s)"
                % exc) from exc
        reference_name = str((reference or {}).get("name") or "").strip().lower()
        try:
            # strict=True : un reçu malformé ou contradictoire refuse la porte
            # au lieu de retomber sur l'auteur Git (R8 B de cx2).
            read, unknown, _correctors = review_authors.reading(
                db, item, milestone, commits, repo,
                families=families, strict=True, families_out=proven_families,
                reference=reference, reference_families=reference_families,
                base=base, head=frozen, target=base, recover=recover,
                admitted_heads=parent_heads)
        except Exception as exc:  # noqa: BLE001 - une lecture illisible ne conclut pas
            raise _ProvenanceError(
                "the authors of the frozen range cannot be read (%s)" % exc) from exc
        for entry in read or ():
            entry = entry if isinstance(entry, Mapping) else {}
            name = str(entry.get("name") or "").strip()
            if name:
                reader_names.add(name.lower())
            add(name, entry.get("family"), entry.get("families"))
        for sha in unknown or ():
            # #2924 B5 : un inconnu qui n'est même pas un commit lisible (un gel
            # antérieur absent du dépôt scratch, un marqueur « freeze:N ») ne
            # doit pas être silencieusement ignoré : la porte refuse fermé.
            fallback = _commit_author(places, sha)
            if not fallback:
                if isinstance(sha, review_authors.PriorFreezeUnknown):
                    # #3835 : un inconnu d'un gel antérieur nomme son gel, son
                    # SHA et son lot, et propose `--supersedes` quand il est
                    # prouvé hors de toute ref.
                    raise _ProvenanceError(
                        review_authors.unknown_refusal([sha], head_sha=frozen))
                raise _ProvenanceError(
                    "historical author unreadable for the unknown %s" % sha)
            add(fallback)
    for name in _merge_authors(repo, item, frozen, base=base, base_repo=base_repo,
                               target=target, target_repo=target_repo, queue=queue,
                               exclude=parent_heads):
        add(name)
    authors: list[dict] = []
    for key, entry in found.items():
        proven = set(entry["families"])
        if not proven:
            # Une entrée du lecteur partagé sans famille unique peut cacher
            # plusieurs familles historiques prouvées (#2681) : le registre
            # courant ne les remplace pas, même pour l'assigné (R9 B de cx2).
            # Une absence de preuve reste distinguable d'une ambiguïté : seule
            # la référence garde alors son repli historique.
            proven = set(proven_families.get(key) or ())
        if len(proven) > 1:
            # #3584 : un ensemble prouvé n'est pas une famille inconnue. On
            # exclut chacune de ses familles au lieu de refuser l'ensemble ;
            # la porte ne devine jamais une famille unique.
            authors.append({"name": entry["name"], "family": "",
                            "families": sorted(proven)})
            continue
        if len(proven) == 1:
            authors.append({"name": entry["name"],
                            "family": next(iter(proven))})
            continue
        if key in reader_names and key != reference_name:
            raise _ProvenanceError(
                "the family of the author %s is not proven; the current "
                "registry cannot replace it" % entry["name"])
        try:
            family = actor_family(db, entry["name"], families=families)
        except RegistryReadError as exc:
            raise _ProvenanceError("the agent registry cannot be read (%s)"
                                   % exc) from exc
        if family:
            authors.append({"name": entry["name"], "family": family})
    return authors, families


_SCP = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$")
_URL = re.compile(r"^([a-z][a-z0-9+.-]*)://(?:[^@/]+@)?([^/:]+)(?::(\d+))?/(.+)$", re.I)
#: the port a scheme names when it names none: `ssh://host:22/x` is `host:x`
_DEFAULT_PORTS = {"ssh": 22, "git+ssh": 22, "ssh+git": 22, "git": 9418, "http": 80,
                  "https": 443}


def normalize_source(source: str) -> str:
    """Normalize a stored queue source, retaining server ports and fork identity."""
    if source.partition("/")[0].split(":")[0].lower() == "github.com":
        return source.lower()
    return source


def source_of(repo: str) -> str:
    """The repository a lot is queued from, as a radar pass compares it: its
    origin as `host/path` (`git@github.com:o/r.git` and
    `https://github.com/o/r` are one source; a checkout recreated keeps it;
    a port other than the scheme's own is kept: two servers on one host are
    two sources), else the real path of the origin or of `repo` itself. Two
    projects that share commits (a fork) are two sources."""
    url = (plan_git._out(repo, "remote", "get-url", "origin") or "").strip()
    if not url:
        return os.path.realpath(repo)
    host, port, path = None, None, ""
    found = _URL.match(url)
    if found and found.group(1).lower() != "file":
        scheme, host, port, path = found.groups()
        if port is not None and int(port) == _DEFAULT_PORTS.get(scheme.lower()):
            port = None
    elif not url.startswith(("/", ".", "file://")) and _SCP.match(url):
        host, path = _SCP.match(url).groups()
    if host is None:
        local = url[len("file://"):] if url.startswith("file://") else url
        return os.path.realpath(os.path.join(repo, local))
    path = path.strip("/")
    path = path[:-len(".git")] if path.endswith(".git") else path
    return normalize_source("%s%s/%s" % (host.lower(), ":%d" % int(port) if port else "", path))


def _unreadable_objects(where: str, revs, base: str | None = None) -> list[str] | None:
    """Les objets que `where` ne peut pas lire pour `revs`.

    `revs` : une tête, ou plusieurs dont on vérifie l'union. Le clone radar
    est un `git clone --shared` : ses objets viennent par alternates du
    checkout des agents, et un élagage de celui-ci (`gc`, ménage disque)
    retire ceux que seul le clone partagé utilisait. Un
    `rev-list --objects --missing=print` nomme les objets absents de la
    fermeture **complète** des têtes : ceux de la cible et de la base de
    fusion comptent aussi, puisque `merge-tree` lit les trois côtés.

    Avec `base` (la cible), le parcours est borné à ce que lit réellement la
    fusion d'essai : l'histoire propre des têtes (`--not base`), puis les
    arbres des trois côtés — têtes, cible et commits de bord (`--boundary`,
    dont les bases de fusion). La fermeture complète coûte 20 à 40 s sur
    Nexlink à chaque fetch, la version bornée moins d'une seconde.

    Une exception : plusieurs bases de fusion. `merge-tree` les fusionne
    récursivement pour construire sa base virtuelle ; l'ancêtre propre de ces
    bases n'apparaît alors ni dans l'histoire propre des têtes ni dans les
    arbres de bord. Dès que `merge-base --all` nomme plus d'une base pour une
    tête, le garde revient à la fermeture complète : mieux vaut un parcours
    lent qu'une tête rendue que la fusion ne peut pas lire.

    `None` quand le parcours lui-même échoue : l'appelant le traite comme un
    manque à réparer, jamais comme une tête lisible."""
    from . import resolutions
    revs = [revs] if isinstance(revs, str) else list(revs)
    revs = list(dict.fromkeys(revs))
    if not revs:
        return []
    for rev in revs:
        if plan_git._commit(where, rev) is None:
            return [rev]      # une tête manquante arrête rev-list : on la nomme
    if base and plan_git._commit(where, base) is None:
        return [base]
    if not base:
        walks = [["rev-list", "--objects", "--missing=print", *revs]]
    else:
        proc = resolutions._run(where, "rev-list", "--boundary", *revs, "--not", base)
        if proc.returncode:
            return None
        boundary = [line[1:] for line in resolutions._text(proc).splitlines()
                    if line.startswith("-")]
        for rev in revs:
            if rev == base:
                continue
            proc = resolutions._run(where, "merge-base", "--all", rev, base)
            if proc.returncode not in (0, 1):
                return None
            if len(resolutions._text(proc).split()) > 1:
                # bases multiples : leur fusion récursive lit leur ancêtre
                walks = [["rev-list", "--objects", "--missing=print",
                          *sorted(set(revs) | {base})]]
                break
        else:
            walks = [
                ["rev-list", "--objects", "--missing=print", *revs, "--not", base],
                ["rev-list", "--objects", "--missing=print", "--no-walk",
                 *sorted(set(revs) | {base} | set(boundary))],
            ]
    missing = []
    for args in walks:
        proc = resolutions._run(where, *args)
        if proc.returncode:
            return None
        for line in resolutions._text(proc).splitlines():
            if not line.startswith("?"):
                continue
            fields = line[1:].split()
            if fields and fields[0] not in missing:
                missing.append(fields[0])
    return missing


def _refetch_tip_objects(where: str, url: str, branch: str) -> str:
    """Refait le fetch de `branch` dans `where`, sans négociation.

    Le clone lit ses objets par alternates : un fetch ordinaire peut répondre
    « à jour » (la tête est lisible par les alternates) ou déposer un pack dont
    `unpack-objects` saute les objets déjà fournis par eux — la tête reste
    alors une ref sur des objets prêtés. `--refetch` force le transfert, et
    `transfer.unpackLimit=1` garde le pack : `index-pack` l'écrit même quand
    les alternates connaissent déjà ses objets, donc le clone les possède.
    La ref de file est retirée d'abord, pour que le fetch ne soit pas un
    simple « à jour ». Le pack couvre l'histoire de la branche (aucune
    négociation) : cette réparation est rare et vaut mieux qu'un lot écarté
    pour un objet absent. `--no-auto-gc` et `--no-auto-maintenance` évitent
    qu'un repack détaché du clone ne coure pendant que la fusion d'essai lit
    ses objets. Rend le message d'erreur de git, vide si le fetch a réussi."""
    from . import resolutions
    name = _remote_branch(branch)
    ref = QUEUE_REF + name
    resolutions._run(where, "update-ref", "-d", ref)
    proc = resolutions._run(where, "-c", "transfer.unpackLimit=1",
                            "fetch", "--quiet", "--no-tags", "--refetch",
                            "--no-auto-gc", "--no-auto-maintenance", url,
                            "+refs/heads/%s:%s" % (name, ref), timeout=FETCH_TIMEOUT)
    return resolutions._err(proc)[:300] if proc.returncode else ""


def _fetch_refusal(where: str, error: str) -> str:
    """#3181 : la suite du refus d'un fetch, selon sa cause. Une corruption
    locale (objet ou ref vides après une coupure de courant) ne se résorbe pas
    en attendant la forge : `resolutions._run` a déjà mis en quarantaine les
    fichiers vides et refait le fetch une fois, l'erreur restante se répare à
    la main dans le clone."""
    from . import resolutions
    if resolutions.local_corruption(error):
        return ("(%s): repair the clone %s and its alternates by hand (git fsck), "
                "waiting for the remote will not fix it"
                % (resolutions.fetch_failure(error)[:300], where))
    return "(%s): try again once the remote answers" % error[:300]


def _clone_base(where: str, base: str | None) -> str | None:
    """Le commit de cible dont les objets sont vérifiés avec ceux des têtes.

    `base` (la cible du même fetch) d'abord, puis la cible déclarée du clone,
    puis sa branche par défaut. La cible et la base de fusion comptent : la
    fusion d'essai lit leur côté de chaque conflit, et un blob d'ancêtre
    commun absent fait échouer `merge-tree` même quand la tête est complète
    (B1 de la relecture #2738). Aucun commit lisible : None, le garde ne
    couvre alors que les têtes fournies."""
    def first(candidates) -> str | None:
        for candidate in candidates:
            if not candidate:
                continue
            commit = plan_git._commit(where, candidate)
            if commit:
                return commit
        return None
    if base:
        name = _remote_branch(base)
        commit = first([QUEUE_REF + name, "origin/" + name, name, base])
        if commit:
            return commit
    declared = plan_git.declared_target(where)
    if declared:
        commit = first([QUEUE_REF + _remote_branch(declared), declared])
        if commit:
            return commit
    try:
        default = plan_git.default_target(where)
    except Exception:  # noqa: BLE001 - cible déclarée introuvable (#2680)
        default = None
    if default:
        commit = first([default, QUEUE_REF + _remote_branch(default)])
        if commit:
            return commit
    return first(plan_git.DEFAULT_TARGETS)


def _guarantee_tip_objects(clone, url: str, names, tips: dict, base: str | None) -> dict:
    """Ne rend que des têtes dont la fermeture est lisible dans le clone.

    #2738 : une tête fraîche peut n'être qu'une ref sur des objets prêtés par
    les alternates ; si la source les a élagués, la fusion d'essai et les
    lectures de gel échouent (« blob absent du clone de calcul ») et
    `paquet-candidats.py` écarte un lot prêt en erreur de lecture. Le garde
    vérifie l'union des objets lus par la fusion d'essai — histoire propre des
    têtes, arbres des têtes, de la cible et des bases de fusion, ou fermeture
    complète si les bases sont multiples —, répare chaque branche
    dont des objets manquent, puis re-vérifie. Une erreur Git du garde devient
    une `QueueError` (B2), jamais une exception qui ferait planter l'appelant."""
    label = ", ".join(names[:5]) + (" …" if len(names) > 5 else "")
    try:
        return _verified_tips(clone, url, names, tips, base)
    except (GitError, OSError) as exc:
        raise QueueError("cannot guarantee the objects of %s in the radar clone (%s): "
                         "retry once the remote and the clone answer"
                         % (label or "the fetched branches", str(exc)[:300])) from exc


def _verified_tips(clone, url: str, names, tips: dict, base: str | None) -> dict:
    """Le corps du garde de `_guarantee_tip_objects` (hors traduction d'erreurs)."""
    base_branch = _remote_branch(base) if base else ""
    base_commit = _clone_base(clone.path, base)

    def wanted() -> list[str]:
        revs = {tips[name] for name in names if tips.get(name)}
        if base_commit:
            revs.add(base_commit)
        return sorted(revs)

    revs = wanted()
    missing = _unreadable_objects(clone.path, revs, base_commit) if revs else []
    if missing == []:
        return tips
    # chemin rare : nommer la branche qui porte chaque objet manquant et la
    # refaire. Une tête peut changer (la branche a bougé) ; les entrées de
    # `tips` de la même branche suivent.
    repaired: set = set()
    for tip in revs:
        branch = next((name for name in names if tips.get(name) == tip), "")
        if not branch and tip == base_commit:
            branch = base_branch
        if not branch or _remote_branch(branch) in repaired:
            continue
        if _unreadable_objects(clone.path, tip) == []:
            continue
        repaired.add(_remote_branch(branch))
        error = _refetch_tip_objects(clone.path, url, branch)
        if error:
            raise QueueError("cannot fetch %s from origin for the objects of its head %s %s"
                             % (branch, tip[:12], _fetch_refusal(clone.path, error)))
        fresh = plan_git._commit(clone.path, QUEUE_REF + _remote_branch(branch)) or tip
        for name in names:
            if _remote_branch(name) == _remote_branch(branch):
                tips[name] = fresh
        if _remote_branch(branch) == base_branch:
            base_commit = fresh
    revs = wanted()
    missing = _unreadable_objects(clone.path, revs, base_commit) if revs else []
    if missing == []:
        return tips
    detail = ("walk impossible" if missing is None
              else "%d unreadable, first %s" % (len(missing), missing[0][:12]))
    label = ", ".join(names[:5]) + (" …" if len(names) > 5 else "")
    raise QueueError(
        "cannot guarantee the objects of %s in the radar clone (%s, after an explicit "
        "fetch from origin): the shared clone reads the agents' objects (git clone "
        "--shared) and they were pruned; fetch the branch again in the source, or "
        "recreate the clone" % (label or "the fetched branches", detail))


def fetch_fresh(repo: str, names, clone=None,
                base: str | None = None) -> tuple[dict, str]:
    """({name: commit, or None if there is no such branch}, repository holding
    them) of the branches `names` (lot branches or targets), read fresh. With
    an `origin`, they are listed on it, then fetched from it now into the
    radar's clone (`clone`, a `resolutions.Replayer`: never into the agents'
    repository), under QUEUE_REF; neither a local branch nor the cached
    `origin/<branch>` counts, since another host may have pushed since. No
    clone, or a remote that does not answer, raises QueueError: no fetch, no
    tip. Without an `origin`, `repo` is the only copy and its local branches
    are the tips.

    #2738 : les objets de chaque tête sont vérifiés dans le clone avant d'être
    rendus, avec ceux de la cible (B1 : la fusion lit la base et l'ancêtre
    commun) ; s'ils manquent (source élaguée, alternates), un `--refetch`
    explicite de la branche les rapatrie, sinon QueueError. `base` nomme la
    cible du même fetch, pour la vérifier et la réparer comme les têtes. Une
    tête rendue est donc lisible par la fusion d'essai et les lectures de
    gel, jamais une ref sur des objets absents."""
    from . import resolutions
    names = sorted({n for n in names if n})
    remote = {n: _remote_branch(n) for n in names}
    # #3041 : l'URL est rejouée dans le clone ; un chemin relatif se lit
    # depuis `repo`.
    url = resolutions._local_from(repo, plan_git._out(repo, "remote", "get-url", "origin") or "")
    if not url:
        return {n: plan_git._commit(repo, "refs/heads/" + remote[n]) for n in names}, repo
    what = "branch%s %s" % ("es" if len(names) > 1 else "", ", ".join(names))
    if clone is None:
        raise QueueError("%s: origin cannot be fetched without the radar's clone "
                         "(ameesh work queue add finds it from the project)" % what)
    present: set = set()
    # #5179 : ce fetch reste hors du verrou de clone (le radar peut l'utiliser
    # longtemps) ; ses refs sont sujettes aux verrous orphelins comme l'index.
    clone.sweep_locks()
    try:
        if not os.path.isdir(os.path.join(clone.path, ".git")):
            clone.ensure()  # an existing clone is not reset: the radar may use it
        else:
            # #2673 : un clone déjà créé sans `ameesh.target` (origin/HEAD figé
            # sur une release) reçoit sa cible ici, sans être réinitialisé.
            clone.declare_target()
        wanted = sorted(set(remote.values()))
        listing = resolutions._run(clone.path, "ls-remote", url,
                                   *("refs/heads/" + b for b in wanted), timeout=FETCH_TIMEOUT)
        failed = resolutions._err(listing) if listing.returncode else None
        if failed is None:
            listed = {line.split("\t", 1)[1] for line in resolutions._text(listing).splitlines()
                      if "\t" in line}
            present = {b for b in wanted if "refs/heads/" + b in listed}
        if failed is None and present:
            proc = resolutions._run(clone.path, "fetch", "--quiet", url,
                                    *("+refs/heads/%s:%s%s" % (b, QUEUE_REF, b)
                                      for b in sorted(present)), timeout=FETCH_TIMEOUT)
            failed = resolutions._err(proc) if proc.returncode else None
    except (GitError, OSError) as exc:
        failed = str(exc)
    if failed is not None:
        raise QueueError("cannot fetch %s from origin %s"
                         % (what, _fetch_refusal(clone.path, failed)))
    tips = {n: plan_git._commit(clone.path, QUEUE_REF + remote[n]) if remote[n] in present
            else None for n in names}
    return _guarantee_tip_objects(clone, url, names, tips, base), clone.path


def branch_tip(repo: str, branch: str, clone=None) -> tuple[str | None, str]:
    """(tip, repository holding it) of a lot branch, read fresh
    (`fetch_fresh`). A branch that origin no longer has raises QueueError:
    the commit the clone kept is not the tip."""
    tips, where = fetch_fresh(repo, [branch], clone)
    if tips[branch] is None and where != repo:
        raise QueueError("cannot fetch branch %s from origin (no such branch there): push "
                         "it again" % branch)
    return tips[branch], where


def target_tip(repo: str, target: str, clone=None) -> tuple[str | None, str]:
    """(tip, repository holding it) of the target branch, read fresh
    (`fetch_fresh`).

    A target that origin no longer has raises QueueError at admission: without
    it, the class was read from the clone's cached `QUEUE_REF` ref and a light
    lot entered, only to be deferred at every pass (`run_pass`: target not
    found) — no merge ever happened, but the queue said otherwise (#2278).
    Without an `origin`, the repository's local branches are the only copy: a
    missing local branch is a missing target too."""
    if not target:
        raise QueueError("no target branch known: the merge queue cannot admit a lot "
                         "without a target (set the lot's target or the project's)")
    tips, where = fetch_fresh(repo, [target], clone)
    if tips[target] is None:
        place = "origin" if where != repo else "the repository"
        raise QueueError("target %s not found on %s: the merge queue refuses a lot whose "
                         "target no longer exists (deleted or renamed). Push the target, point "
                         "the lot at an existing branch, then ameesh work queue add the lot "
                         "again" % (_remote_branch(target), place))
    return tips[target], where


#: #3974 : durée au plus de la lecture d'auctorialité d'un lot à l'entrée de
#: file (balayages `git grep` de `review_authors`) ; au-delà, refus explicite
#: qui nomme le lot et l'étape, jamais 25 min sans sortie. Les balayages faits
#: restent dans le cache de l'hôte : l'essai suivant repart plus loin.
AUTHORS_SWEEP_BUDGET_S = 300.0


def readiness(db, lot: int, repo: str, **kwargs) -> dict:
    """`_readiness`, la lecture d'auctorialité bornée (#3974)."""
    from . import review_authors
    with review_authors.sweep_deadline(AUTHORS_SWEEP_BUDGET_S,
                                       "lot #%d" % int(lot)) as sweep:
        try:
            ready = _readiness(db, lot, repo, **kwargs)
        except review_authors.SweepDeadline as exc:
            raise QueueError("%s; retry: the sweeps already done are cached on "
                             "this host" % exc) from exc
        except QueueError as exc:
            if not sweep.tripped:
                raise
            raise QueueError("lot #%d: the authorship sweep exceeded %d s at step "
                             "%s (%s); retry: the sweeps already done are cached "
                             "on this host" % (int(lot), int(AUTHORS_SWEEP_BUDGET_S),
                                               sweep.tripped, exc)) from exc
        if sweep.tripped:
            # une couche a lu l'échéance comme un doute : rien n'est prouvé
            raise QueueError("lot #%d: the authorship sweep exceeded %d s at step "
                             "%s; retry: the sweeps already done are cached on "
                             "this host" % (int(lot), int(AUTHORS_SWEEP_BUDGET_S),
                                            sweep.tripped))
    return ready


def _readiness(db, lot: int, repo: str, *, ci=None, clone=None, project=None,
               commit_ci=None, full_ci=None, cfg=None, parts=None,
               packet_exception=None) -> dict:
    """Checks that `lot` may enter its queue. Returns `{target, frozen_sha,
    pr, class, class_why, waived}`; QueueError says what is missing. Commit ids
    are resolved to full ones before being compared; `ci` (`radar.github_ci(...,
    strict=True)`) must report the PR green on the frozen commit itself, and
    an error reading it refuses the lot. A project with `ci_required` (L160g)
    also needs GitHub's CI green on the frozen commit itself, PR or not
    (`commit_ci`, `radar.commit_ci` by default).

    L539 (§8): a **light** lot needs no review verdict — its class comes from
    `lot_class.of_lot` on the frozen diff — but it still needs a green CI on
    the frozen head, a non-draft PR, and a target that is not `release/*`.
    Any doubt about the class (unreadable canon, unreachable diff, `cfg`
    absent) leaves the verdict requirement in place; the class and its reason
    are traced in the queue note. The target resolved here (`branch_target`,
    then the project's, then the repository's default) carries through the
    classification and the admission (revue #544 r2), and must exist **fresh**
    on origin: an absent target is refused here, not deferred at every pass
    (#2278, `target_tip`).

    #2207 : la branche **et la cible** sont récupérées fraîches dans le clone
    du radar ; la classe, comme toutes les lectures comparées, se lit contre
    ce même commit de cible, jamais contre la cible locale d'un checkout.

    #2281 : un lot dont le pilote a marqué « verdict non requis » (#1979,
    `reviewer = -none`) entre sans avis, comme le veut la marque ; le gel
    d'une autre tête la périme (stockage) et un verdict bloquant courant la
    surclasse. `waived` le dit à la note de file.

    L756 (#566): a lot of class **release** must additionally certify its
    frozen head with a **full-suite** run — a push or a manual dispatch,
    never the impacted-only plan of a pull request (`full_ci`,
    `radar.full_suite_ci` by default when the project declares a GitHub
    repository).

    #1374 : les avis passent par la porte partagée `review_gate` — comptés par
    classe, indépendants (familles distinctes, aucune celle de l'auteur) et
    sans blocage non levé ; la même porte que le feu du pilote.

    The CI is required **explicitly**: `ci=None` without `ci_required` is
    refused (no reader). When the PR reader reports no PR, the frozen
    commit's CI certifies admission instead (#3080), including the declared
    statuses and due parts. An unreadable PR, a moved head or a red PR never
    falls back to a green commit.

    L1651 (#1650): a project that declares `ci_parts` also proves the
    **completeness** of the frozen CI — the exact set of parts the change set
    makes due, recomputed by the project's versioned selector against the
    target checked here (`_parts_reader`), each one present and
    terminal-success on the frozen commit. `parts` (a callable returning the
    due contexts, or a sequence) replaces that reader: a caller that
    requalifies a lot — the §12 package manifest of the integration train
    (#1467), where the package's full-suite CI substitutes for the individual
    ones — passes what it vouches for, and `parts=()` disables the per-lot
    proof for that gesture. Neither default is a fallback: an unreadable
    contract refuses."""
    from . import lot_class
    from . import plan_github, radar
    row = work.get(db, lot)
    if row is None:
        raise QueueError("lot #%d not found" % lot)
    target = row.get("branch_target") or getattr(project, "target", "") or plan_git.default_target(repo)
    glob = radar.protected_target(project, target)
    if glob:
        # L160g (#473): main, release branches and tags stay an orchestrator's gesture
        raise QueueError("%s is a protected target of project %s (%s): the merge queue "
                         "never merges there; it is the orchestrator's gesture"
                         % (target, project.name, glob))
    if row.get("state") in work.MERGED_STATES or row.get("state") == "closed":
        raise QueueError("lot #%d is %s: nothing to merge" % (lot, row["state"]))
    if not row.get("branch"):
        raise QueueError("lot #%d has no branch (ameesh work assign %d <agent> --branch …)"
                         % (lot, lot))
    marks = work.milestones(db, lot)
    freeze = plan_git.latest_freeze(marks)
    if not freeze or not freeze.get("sha"):
        raise QueueError("lot #%d is not frozen: ameesh work milestone %d frozen --sha <commit>"
                         % (lot, lot))
    # #2207 : la cible est récupérée avec la branche, dans le même fetch. Le
    # clone `--shared` a pris `origin/<cible>` du checkout des agents, qui peut
    # être en retard : sans cette récupération, la classe et la fusion se
    # lisaient sur une cible périmée (P2-1 de la R1).
    names = [row["branch"]] + ([target] if target else [])
    try:
        tips, where = fetch_fresh(repo, names, clone, base=target)
    except QueueError as exc:
        raise QueueError("cannot fetch branch %s%s from origin: %s"
                         % (row["branch"], " and target %s" % target if target else "",
                            str(exc))) from exc
    tip = tips.get(row["branch"])
    if not tip:
        if where != repo:
            raise QueueError("cannot fetch branch %s from origin (no such branch there): push "
                             "it again" % row["branch"])
        raise QueueError("branch %s of lot #%d is neither local nor on origin"
                         % (row["branch"], lot))
    fresh_target = tips.get(target) if target else None
    frozen = pinned(where, freeze["sha"])
    if frozen is None and not _HEX.fullmatch(freeze["sha"].strip().lower()):
        raise QueueError("the freeze of lot #%d names %r, not a commit id: freeze it again "
                         "(ameesh work milestone %d frozen --sha %s)"
                         % (lot, freeze["sha"], lot, tip))
    if not _same(tip, frozen):
        raise QueueError("branch %s is at %s, not at its freeze %s%s: freeze it again "
                         "(ameesh work milestone %d frozen --sha %s), then have it reviewed"
                         % (row["branch"], tip[:12], freeze["sha"][:12],
                            "" if frozen else " (not a commit of this repository)", lot, tip))
    # #2278 : la cible doit exister fraîchement, sinon `_class_of` relit la
    # ref gardée dans le clone et admet le lot pour un report éternel. #2207
    # l'a récupérée avec la branche : c'est `fresh_target` qui fait foi.
    if not target:
        raise QueueError("no target branch known: the merge queue cannot admit a lot "
                         "without a target (set the lot's target or the project's)")
    if fresh_target is None:
        place = "origin" if where != repo else "the repository"
        raise QueueError("target %s not found on %s: the merge queue refuses a lot whose "
                         "target no longer exists (deleted or renamed). Push the target, point "
                         "the lot at an existing branch, then ameesh work queue add the lot "
                         "again" % (_remote_branch(target), place))
    # #2333 (canon §11 bis) : une tête de travail (`lot/**`, `agent/**`,
    # `review/**`) n'entre pas dans la file d'une cible d'intégration hors
    # paquet. L'admission refuse tôt, avec le message §11 bis ; `land` la
    # re-vérifie au dernier geste pour les lignes déjà en file. Une exception
    # déclarée au journal du lot pour cette tête exacte passe, comme une tête
    # `integration/**` (le paquet). `packet_exception` porte l'exception déjà
    # validée par un appelant en simulation (`merge-pr --dry-run`) : elle
    # autorise sans être journalisée (relecture #2333 N1 : un dry-run n'écrit
    # rien), la lecture du journal restant la règle par défaut.
    guard_db, kind, reason = db, "", ""
    if packet_exception:
        guard_db, (kind, reason) = None, packet_exception
    try:
        packet_guard.check_merge(guard_db, lot, row["branch"], target, tip,
                                 exception=kind, reason=reason)
    except packet_guard.PacketGuardError as exc:
        raise QueueError(str(exc)) from exc
    klass = _class_of(db, lot, row, repo, where, target, tip, cfg=cfg,
                      base=fresh_target)
    # #1374 : la même porte que le feu — avis comptés par classe, indépendants
    # (familles), et aucun blocage non levé ; un `ok` récent ne masque plus le
    # `blocked` d'un autre relecteur (P1-1). #2281 : la dispense de re-verdict
    # du pilote (#1979) lève l'exigence d'avis sur cette tête gelée. #1515 :
    # la porte lit tous les auteurs du lot — détenteurs du journal, auteurs
    # du delta gelé et correcteurs de relecteur (#2739/#2888) —, pas seulement
    # l'assigné courant.
    gate_authors = {}
    if required_verdicts(klass) > 0:
        # #2803/#2766 (B cx1) : sur un véhicule de paquet, la porte borne
        # elle-même la lecture au delta propre relu (`packet_reviews.need`).
        # Pré-lire toute la plage du paquet agrège les familles des membres
        # déjà relus et peut lever « famille ambiguë » avant que le périmètre
        # ne soit déterminé : la lecture est reportée à la porte, qui la borne.
        from . import packet_reviews
        if not packet_reviews.is_vehicle(row):
            # La marque de dispense est relue par la porte après les jalons
            # (#2281) : on lit donc les auteurs même si la copie courante porte
            # une dispense, pour qu'une marque périmée ne laisse pas la porte sur
            # le seul assigné (#1515).
            try:
                authors, families = _complete_authors(
                    db, row, tip, where, fresh_target or target, target_repo=repo,
                    queue=(where != repo), klass=klass, marks=marks, cfg=cfg)
            except _ProvenanceError as exc:
                raise QueueError(
                    "lot #%d: the complete authors of the lot cannot be established "
                    "(%s), so no verdict can be proven independent (canon §2)"
                    % (lot, exc)) from exc
            gate_authors = {"authors": authors, "families": families,
                            "authors_complete": True}
    gate = review_gate(db, marks, tip, repo=where, klass=klass, item=row,
                       cfg=cfg, target=fresh_target, **gate_authors)
    if not gate["ok"]:
        raise QueueError("lot #%d: %s" % (lot, gate["why"]))
    # L756 (#566): un lot de classe release ne se contente pas du check de sa
    # PR — une PR de version seule ne lance rien, et une PR peut n'avoir suivi
    # que le plan impacté. Il lui faut une exécution de la suite complète
    # (poussée ou lancement manuel) verte sur la tête gelée exacte.
    if klass.get("class") == lot_class.RELEASE:
        required_full_ci(project, tip, full_ci, lot=lot)
    needs_commit_ci = project is not None and getattr(project, "ci_required", False)
    status = None
    if ci is None:
        if not needs_commit_ci:
            raise QueueError("lot #%d has no CI reader: a green CI on the frozen head %s is "
                             "required before entering the queue" % (lot, tip[:12]))
    else:
        try:
            status = ci(row["branch"])
        except plan_github.GithubError as exc:
            raise QueueError("cannot read the CI of branch %s on GitHub (%s): try again once "
                             "gh works" % (row["branch"], exc)) from exc
        if not status:
            # #3080: commit statuses (e.g. lot/*) do not need an open PR.
            needs_commit_ci = True
    if status:
        if not _same(status.get("head"), tip):
            raise QueueError("PR #%s is at %s, not at the freeze %s: its CI does not "
                             "vouch for the frozen commit" % (status.get("number"),
                                                             (status.get("head") or "?")[:12],
                                                             tip[:12]))
        if not status.get("green"):
            raise QueueError("the CI of PR #%s is not green" % status.get("number"))
        if (gate["required"] == 0 or gate["waived"]) and status.get("draft"):
            raise QueueError("PR #%s is a draft: a lot without a review verdict merges only "
                             "from a non-draft PR" % status.get("number"))
    proof = None
    if needs_commit_ci:
        project = project or radar.Project(name=os.path.basename(repo), repo=repo)
        if parts is None and getattr(project, "ci_parts", None):
            parts = _parts_reader(project, repo, target, tip, clone, cfg=cfg)
        _required_ci(project, tip, commit_ci, parts=parts, branch=row["branch"])
        proof = getattr(parts, "proof", None)
    return {"target": target, "frozen_sha": tip, "frozen_id": freeze["id"],
            "pr": (status or {}).get("number"),
            "class": klass.get("class") or gate.get("class"),
            "class_why": klass.get("why") or "", "vehicle": _vehicle_note(gate),
            "parts": proof,
            "waived": bool(gate.get("waived")),
            "waived_by": gate.get("waived_by") or ""}


def _vehicle_note(gate: dict) -> str:
    """La preuve du véhicule de paquet, nommée à part de la classe (#4041).

    Sur un véhicule (§12, #2803), la porte lit les avis des membres sur leur
    tête gelée et ne compte sur le véhicule que les avis de son delta propre.
    La classe affichée reste celle du diff gelé entier ; cette note dit
    pourquoi la porte n'a pas exigé ses avis.
    """
    why = str(gate.get("vehicle_why") or "")
    if not why:
        return ""
    required = int(gate.get("required") or 0)
    return "§12 vehicle proven by its members: %s; %d verdict%s on the vehicle (#2803)" % (
        why, required, "" if required == 1 else "s")


def _class_of(db, lot: int, row: dict, repo: str, where: str | None, target: str | None,
              tip: str, *, cfg=None, base: str | None = None) -> dict:
    """La classe du lot pour la file (L539) : lue là où la tête gelée vit.

    La classe se lit entre la cible **fraîche** et la tête gelée. Seul `base`
    est une cible fraîche : le commit que l'appelant vient de récupérer
    (`readiness`), ou `target` quand il nomme déjà un commit. Ce commit est
    aussi la révision exacte du diff (`base_sha`, #2318), jamais une ref que le
    fetch concurrent du radar peut déplacer entre la lecture et la classe.
    Sinon, la borne
    est la cible que le radar a laissée sous `QUEUE_REF` dans son clone, à défaut
    `origin/<cible>` du dépôt des agents, puis sa cible locale : c'est la
    cible du **dernier** fetch connu, que rien ne prouve fraîche — un fetch
    antérieur, une cible supprimée depuis sur origin (le fetch ne nettoie pas)
    ou déjà avancée par la fusion du lot. La lecture garde sa classe, mais son
    `why` le dit (#2379). La copie `origin/<cible>` du clone `--shared` n'est
    jamais une borne : c'est la cible locale périmée de sa source (#2207, P2-1).

    Toutes les lectures comparées portent sur **la même** cible fraîche : une
    source qui ne la porte pas (référence périmée, objet absent) est écartée
    plutôt que comparée avec une autre borne — un `main` en retard ajouterait
    des commits déjà fusionnés, dont des chemins sensibles, et surclasserait
    la lecture fraîche (#2207, P2-2). Entre lectures de même borne, la plus
    haute l'emporte, une lecture calculée ne se rétrograde jamais devant une
    autre, et un doute ne se lit pas comme une classe sensible (revue #544 r1).

    `cfg` absent ou dépôt illisible : `lot_class.of_lot` rend un doute
    (`sensitive`, drapeau `doubt`), et la file garde l'exigence de verdict."""
    from . import lot_class
    # La borne de diff peut être un commit ou un ref de file : l'axe release se
    # lit sur la cible résolue, que la ligne du lot ne porte pas toujours.
    item = row
    if target and not row.get("branch_target"):
        item = dict(row, branch_target=target)
    paths = []
    if target:
        source = where or repo
        cloned = bool(where and where != repo)
        fresh = base
        if fresh is None and cloned:
            ref = _class_base(where, target, queue=True)
            fresh = plan_git._commit(where, ref) if ref else None
        if fresh is None:
            ref = _class_base(repo, target)
            fresh = plan_git._commit(repo, ref) if ref else None
        repos = [source] + ([repo] if cloned else [])
        for path in repos:
            # Une source ne parle du lot que si elle porte les deux bornes :
            # sa cible peut être en retard et ses objets absents (le fetch de
            # `readiness` n'écrit que dans le clone).
            if fresh and plan_git._commit(path, fresh) and plan_git._commit(path, tip):
                paths.append((path, _class_ref(path, target, fresh,
                                               queue=cloned and path == source), fresh))
        if not paths:
            # aucune cible fraîche lisible : repli explicite sur les refs de
            # cible disponibles. Ce repli ne prouve pas la fraîcheur : un
            # `QUEUE_REF/<cible>` laissé par un fetch antérieur survit à la
            # disparition de la cible sur origin (le fetch ne le supprime pas),
            # et `of_lot` peut alors rendre une classe calculée, `light`
            # comprise, au lieu d'un doute. L'admission doit refuser une cible
            # absente (#2278) ; ce repli ne le garantit pas (#2284).
            paths.append((source, _class_base(source, target, queue=cloned) or target, None))
            if cloned:
                paths.append((repo, _class_base(repo, target) or target, None))
    else:
        paths.append((repo, target, None))
    best = None
    for path, base_ref, base_sha in paths:
        current = lot_class.of_lot(db, lot, cfg=cfg, item=item, repo=path, base=base_ref,
                                   head=tip, base_sha=base_sha)
        if best is None or _class_worse(current, best):
            best = current
    if (target and base is None and best is not None
            and not _HEX.fullmatch(target.strip().lower())):
        best = dict(best, why="%s; borne %s non prouvée fraîche (dernier fetch connu, "
                    "aucune cible récupérée par l'appelant)"
                    % (best.get("why") or "?", best.get("target") or target))
    return best


def _class_worse(candidate: dict, best: dict) -> bool:
    """La plus haute des deux lectures l'emporte ; à classe égale, une lecture
    calculée explique mieux qu'un doute."""
    from . import lot_class
    def rank(result):
        return lot_class.RANK.get(str(result.get("class") or ""),
                                  lot_class.RANK[lot_class.SENSITIVE])
    if rank(candidate) != rank(best):
        return rank(candidate) > rank(best)
    return bool(best.get("doubt")) and not candidate.get("doubt")


def _parts_reader(project, repo: str, target: str, sha: str, clone, cfg=None):
    """L1651 (#1650): le lecteur des parts dues d'une tête gelée.

    Le contrat est celui du projet (`ci_parts`), versionné à `ci_parts.rev`
    (la cible à défaut) et **relié à la cible réellement contrôlée** : une
    déclaration qui épingle une autre branche que la cible de cette admission
    ou de cet atterrissage refuse — le contrat d'une autre cible ne prouve pas
    celle-ci. Le lecteur va chercher la référence à chaque appel
    (`fetch_fresh`), donc à chaque contrôle, et recalcule les parts dues avec
    le sélecteur du dépôt (`ci_parts.due`) ; toute incertitude devient un
    `QueueError`, jamais un repli sur les statuts présents."""
    from . import ci_parts
    plan = project.ci_parts
    wanted = _remote_branch(target or getattr(project, "target", ""))
    if plan.rev and _remote_branch(plan.rev) != wanted:
        raise QueueError("the ci_parts contract of project %s is versioned at %s, the target "
                         "checked here is %s: the due parts of this target cannot be proven "
                         "(align ci_parts.rev with the target, or requalify the admission)"
                         % (project.name, plan.rev, target or "<none>"))
    rev = plan.rev or wanted

    def reader():
        tips, holder = fetch_fresh(repo, [rev], clone)
        base = tips.get(rev)
        if not base:
            raise QueueError("the versioned selection source %s of project %s is not on origin: "
                             "the due parts of the frozen head %s cannot be proven"
                             % (rev, project.name, sha[:12]))
        try:
            #: N2 de #1666 : le cache de l'hôte (par couple base/tête gelée)
            #: évite de réextraire l'arbre de la cible à chaque contrôle.
            proof = ci_parts.due(project, sha, base, holder, cache=ci_parts.cache_dir(cfg))
        except ci_parts.PartsError as exc:
            raise QueueError("the parts the change set makes due on the frozen head %s cannot "
                             "be proven: %s" % (sha[:12], exc)) from exc
        reader.proof = proof
        return proof["due"]
    #: L1651 : la provenance de la dernière preuve (`ci_parts.due`), pour la
    #: note de file et le raccord du train (#1467) — jamais un vert sans elle
    reader.proof = None
    return reader


def _required_ci(project, sha: str, commit_ci=None, *, parts=None,
                 branch: str | None = None,
                 allow_inconclusive: bool = False) -> None:
    """GitHub CI on frozen `sha`: `ci_required` or no PR (#3080).
    QueueError otherwise (no run, running, red, unreadable).

    #3085 (règle owner du 23/10) : `allow_inconclusive` n'est passé que par
    la porte du feu/paquet (`fire.ready_pr`) — une lecture `acceptable` (tous
    les checks ont conclu, aucun négatif) suffit alors, un job sauté ou
    annulé pour quota est consigné au lieu de bloquer. La file et `merge-pr`
    gardent la preuve stricte (défaut).

    L1651 (#1650): `parts` (a callable returning the due contexts, or a
    sequence; None: none) closes the completeness hole. Each due part must
    exist in `commit_ci`'s `statuses` — the commit **status** space of that
    exact commit — and be terminal success: a part never dispatched leaves no
    status, and a `skipped` or `cancelled` attempt is not a green one. N1 de
    #1666 : lire `contexts` (qui mêle runs, checks et statuts) laissait un run
    ou un check portant le nom d'une part tenir lieu de part ; la preuve se
    fait désormais dans l'espace des statuts seulement. No status of another
    commit — the reader is pinned to `sha` — can stand for a part of this
    one."""
    from . import plan_github, radar
    # #3560 : le texte `ci_required` est celui que `queue_add` reconnaît
    # (`storage.interface.CI_REQUIRED_EJECTION`) ; « no PR » ne l'est pas.
    required = (storage.interface.CI_REQUIRED_EJECTION
                if getattr(project, "ci_required", False)
                else "requires CI on the frozen commit (no PR)")
    try:
        runs = (commit_ci or (lambda s: radar.commit_ci(project, s, branch=branch)))(sha)
    except plan_github.GithubError as exc:
        raise QueueError("project %s %s, unreadable (%s): try again once gh works"
                         % (project.name, required, exc)) from exc
    if not runs["runs"]:
        raise QueueError("project %s %s: no run on %s yet (push it, let the workflows run)"
                         % (project.name, required, sha[:12]))
    if allow_inconclusive and parts is None and runs.get("acceptable"):
        # #3085 : tous les checks ont conclu, aucun n'est négatif ; les non
        # concluants se consignent, la porte du feu/paquet ne les attend pas.
        return
    if runs["pending"]:
        raise QueueError("the CI on the frozen commit %s is still running (%s)"
                         % (sha[:12], ", ".join(runs["pending"])))
    if runs["failed"]:
        raise QueueError("the CI on the frozen commit %s is red (%s)"
                         % (sha[:12], ", ".join(runs["failed"])))
    if runs.get("missing"):
        raise QueueError("the frozen commit %s carries no successful %s status: its "
                         "required validation has not concluded (required_statuses)"
                         % (sha[:12], " / ".join("%s*" % p for p in runs["missing"])))
    if not runs.get("green"):
        raise QueueError("the CI on the frozen commit %s is not green" % sha[:12])
    if parts is None:
        return
    due = list(parts() if callable(parts) else parts)
    #: N1 de #1666 : l'espace des **statuts de commit** seulement. Un lecteur
    #: qui ne rend pas `statuses` (dict sans la clé, cache d'un autre format)
    #: ne prouve aucune part : jamais de repli sur `contexts`.
    statuses = runs.get("statuses") or {}
    missing = [part for part in due if statuses.get(part) != "success"]
    if missing:
        detail = ", ".join("%s (%s)" % (part, statuses.get(part) or "no status")
                           for part in missing)
        raise QueueError("the CI on the frozen commit %s does not prove the parts the change "
                         "set makes due (%d of %d missing or not successful: %s)"
                         % (sha[:12], len(missing), len(due), detail))


def required_full_ci(project, sha: str, full_ci=None, *, lot=None) -> None:
    """L756 (#566): a release lot certifies its frozen head with a **full
    suite** run — a push or a manual dispatch, never the impacted-only plan of
    a pull request, and never the empty check of a version-only PR. The suite
    is the one the project **declares** (`radar.projects.<p>.full_suite`,
    workflow files resolved to identities): each declared workflow must have
    run on the exact head, green, its jobs included (`skipped`/`neutral`/unread
    jobs certify nothing). QueueError otherwise (no run, running, missing, red,
    unreadable); `full_ci` reads the runs (`radar.full_suite_ci` by default
    when the project has a GitHub repo). Une suite non **déclarée** est refusée
    pour ce qu'elle est — la clé `radar.projects.<p>.full_suite` manquante —,
    jamais comme une lecture gh impossible (#886)."""
    from . import plan_github, radar
    who = "lot #%s" % lot if lot is not None else "this lot"
    reader = full_ci
    if reader is None:
        if project is None or not getattr(project, "github", ""):
            raise QueueError("%s is class release: no full-suite reader (project without a "
                             "GitHub repository) — a full run on the frozen head %s is required"
                             % (who, sha[:12]))
        if not tuple(getattr(project, "full_suite", ()) or ()):
            # #886 (P3, R2 mty-amh-qa-01 sur PR manaty/ameesh#633) : une
            # déclaration manquante n'est pas une panne de lecture ; le message
            # nomme la clé et ne renvoie pas à « once gh works ».
            raise QueueError("%s is class release: project %s declares no full suite "
                             "(radar.projects.%s.full_suite — the workflow files of the full "
                             "run): a green full-suite run on the frozen head %s is required"
                             % (who, project.name, project.name, sha[:12]))
        reader = lambda one: radar.full_suite_ci(project, one)
    try:
        runs = reader(sha)
    except plan_github.GithubError as exc:
        raise QueueError("cannot read the full-suite runs on the frozen head %s (%s): try again "
                         "once gh works" % (sha[:12], exc)) from exc
    if not runs or not runs.get("runs"):
        raise QueueError("%s is class release: no full-suite run (push or manual dispatch) on "
                         "the frozen head %s yet (push it, let the workflow run)"
                         % (who, sha[:12]))
    if runs.get("pending"):
        raise QueueError("the full suite on the frozen head %s is still running (%s)"
                         % (sha[:12], ", ".join(runs["pending"])))
    if runs.get("missing"):
        raise QueueError("%s is class release: the declared full suite did not run on the "
                         "frozen head %s (%s) — a green run of each declared workflow is "
                         "required" % (who, sha[:12], ", ".join(runs["missing"])))
    if runs.get("failed"):
        raise QueueError("the full suite on the frozen head %s is red (%s)"
                         % (sha[:12], ", ".join(runs["failed"])))
    if not runs.get("green"):
        raise QueueError("the full suite on the frozen head %s is not green" % sha[:12])
    return runs


def enter(db, lot: int, *, repo: str, actor: str = "", ci="github", project=None,
          clone=None, commit_ci=None, full_ci=None, cfg=None, parts=None,
          withdrawn_after: float | None = None) -> dict:
    """Puts `lot` into the queue of its target, once `readiness` passes.
    `parts` is `readiness`'s (L1651): the caller that requalifies a lot (§12
    package manifest) passes the due contexts it vouches for, `()` to disable
    the per-lot proof for that gesture.

    #3532: `withdrawn_after` (epoch instant) is the gesture's own date — the
    fire's grant for its fallback. A take-out written since then is final
    for that gesture: `QueueWithdrawn`, checked again in the insertion's
    transaction. #3550: only a take-out of the same target and freeze binds
    the gesture; an old entry of another target or freeze does not."""
    # L165 (EP4) : l'entrée en file est un geste du pilote hors base (la file
    # poussera). Le bail élu est vérifié avant les lectures de préparation et
    # journalisé dans la transaction de l'écriture ; le radar et un humain ne
    # sont pas concernés (`gate` rend None).
    from . import pilot_gesture
    fence = pilot_gesture.gate(cfg, db, "queue", "queue add #%d" % lot,
                               project=getattr(project, "name", None))
    if ci == "github":
        from . import radar
        ci = radar.github_ci(project or radar.Project(name="", repo=repo), strict=True)
    ready = readiness(db, lot, repo, ci=ci, clone=clone, project=project,
                      commit_ci=commit_ci, full_ci=full_ci, cfg=cfg, parts=parts)
    proof = ready.get("parts") or {}
    # L1651 (#1650) : la note garde la provenance de la preuve de complétude
    # (sélecteur versionné et commit de la cible), pour le raccord du train
    # (#1467/1616) — la preuve n'est jamais un simple « vert »
    parts_note = ""
    if proof:
        parts_note = ", parts %s@%s (%d due)" % (proof.get("selector") or "?",
                                                 (proof.get("base") or "?")[:12],
                                                 len(proof.get("due") or ()))
    # #2281 : la dispense de re-verdict du pilote (#1979) est nommée dans la
    # note de file, avec l'auteur du geste lu au journal du lot.
    vehicle_note = ", %s" % ready["vehicle"] if ready.get("vehicle") else ""
    waived_note = ""
    if ready.get("waived"):
        waived_note = ", verdict waived by %s (#1979)" % (
            ready.get("waived_by") or "pilot")

    guard = {} if withdrawn_after is None else {"withdrawn_after": withdrawn_after}

    def add(tx):
        return storage.of(tx).work.queue_add(
            lot, target=ready["target"], frozen_sha=ready["frozen_sha"],
            frozen_id=ready["frozen_id"], source=source_of(repo),
            note="merge queue of %s: queued at %s%s, class %s%s%s%s%s" % (
                ready["target"], ready["frozen_sha"][:12],
                ", PR #%s green" % ready["pr"] if ready["pr"] else "",
                ready.get("class") or "?", " (%s)" % ready["class_why"]
                if ready.get("class_why") else "", vehicle_note, parts_note,
                waived_note),
            actor=actor, **guard)

    if fence is None:
        row = add(db)
    else:
        with db.transaction() as tx:
            fence.hold(tx)
            row = add(tx)
    if row is None:
        if withdrawn_after is not None:
            at = storage.of(db).work.queue_withdrawn_since(
                lot, withdrawn_after, target=ready["target"],
                frozen_id=ready.get("frozen_id"))
            if at is not None:
                raise QueueWithdrawn("lot #%d was taken out of the queue of %s at %s"
                                     % (lot, ready["target"], _utc(at)))
        raise QueueError("lot #%d is no longer open" % lot)
    # L539 : la classe lue à l'entrée, rendue avec la ligne de file (lecture)
    row["class"] = ready.get("class")
    row["class_why"] = ready.get("class_why")
    row["vehicle"] = ready.get("vehicle") or ""
    row["waived"] = bool(ready.get("waived"))
    return row


def leave(db, lot: int, *, actor: str = "", reason: str = "") -> bool:
    from . import pilot_gesture
    fence = pilot_gesture.gate(None, db, "queue", "queue remove #%d" % lot)
    if fence is None:
        return storage.of(db).work.queue_remove(
            lot, note="merge queue: taken out%s" % (" (%s)" % reason if reason else ""),
            actor=actor)
    with db.transaction() as tx:
        fence.hold(tx)
        return storage.of(tx).work.queue_remove(
            lot, note="merge queue: taken out%s" % (" (%s)" % reason if reason else ""),
            actor=actor)


def live_train(cfg, db, lot: int) -> dict | None:
    """#3532: the live train whose plan carries `lot` (`{key, stamp, phase}`),
    None otherwise. Its withdrawal condemns that train: `queue remove` says so
    without refusing. Best effort: an unreadable train is no warning."""
    from .train import TERMINAL
    try:
        ops = storage.of(db).work
        row = next((r for r in ops.queue() if int(r["work_item_id"]) == lot), None)
        if row is None:
            return None
        for name in sorted(declared_projects(cfg)):
            key = "%s:%s" % (name, _remote_branch(row["target"]))
            state = ops.train_read(key) or {}
            members = (state.get("plan") or {}).get("members") or ()
            if state.get("phase") not in TERMINAL and \
                    any(int(m.get("work_item_id") or 0) == lot for m in members):
                return {"key": key, "stamp": state.get("stamp"), "phase": state.get("phase")}
    except Exception:  # noqa: BLE001 - an advisory read never blocks the gesture
        return None
    return None


# --------------------------------------------------------------------------
# order
# --------------------------------------------------------------------------

def _priority(row: dict) -> int:
    return int(row.get("priority") or 2)


def pair_states(report: dict) -> dict:
    """{frozenset(lots): pair} of the radar report."""
    return {frozenset(p["lots"]): p for p in report.get("pairs") or ()}


def order(entries: list[dict], pairs: dict) -> list[dict]:
    """The queued lots of one target in merge order: `[{work_item, rank,
    reason, waits: [(other, resolver)]}]`. `pairs`: `pair_states`."""
    ids = [int(e["work_item_id"]) for e in entries]

    def conflicts(lot: int) -> dict:
        out = {}
        for other in ids:
            pair = pairs.get(frozenset((lot, other))) if other != lot else None
            if pair and pair["state"] in UNPREPARED + ("prepared",):
                out[other] = pair
        return out

    def klass(lot: int) -> int:
        found = conflicts(lot)
        if not found:
            return 0
        return 2 if any(p["state"] in UNPREPARED for p in found.values()) else 1

    ranked = sorted(entries, key=lambda e: (_priority(e), klass(int(e["work_item_id"])),
                                            e.get("queued_ts") or 0, int(e["work_item_id"])))
    out = []
    ahead: list[int] = []
    for n, e in enumerate(ranked, 1):
        lot = int(e["work_item_id"])
        found = conflicts(lot)
        waits = [(other, (p.get("resolver") or {}).get("lot"))
                 for other, p in sorted(found.items())
                 if other in ahead and p["state"] in UNPREPARED]
        if waits:
            reason = "waits for the resolution of its conflict with %s" % ", ".join(
                "#%d" % o for o, _ in waits)
        elif found and any(o in ahead for o in found):
            reason = "conflicts with %s prepared" % ", ".join(
                "#%d" % o for o in sorted(found) if o in ahead)
        else:
            reason = "priority %d, %s" % (_priority(e), "no conflict in the queue"
                                          if not found else "conflicts with lots behind it")
        out.append({"work_item": lot, "rank": n, "reason": reason, "waits": waits})
        ahead.append(lot)
    return out


# --------------------------------------------------------------------------
# stacked replay in the radar's clone
# --------------------------------------------------------------------------

def stack(replayer, target_head: str, lots: list[dict], rows: list[dict], *, generated=()) -> dict:
    """Replays the queue stacked: from `target_head`, merges each lot of
    `lots` (`{work_item, tip}`, in order) with `rerere` primed by the
    resolutions (`rows`) of this lot with the lots already stacked. A lot
    that leaves a conflict is skipped. Returns `{lot: {state: clean|
    replayed|conflict|generated, files}}`. Generated conflicts are excluded
    before rerere; each trial tree is regenerated and checked.

    #2428: the whole trial holds the radar clone's worktree lock. A landing
    running in another process owns the same worktree: the trial waits for
    it, and on timeout raises `resolutions.CloneBusy` instead of checking
    out the target under the merge's gates."""
    with replayer.locked():
        return _stack(replayer, target_head, lots, rows, generated=generated)


def _stack(replayer, target_head: str, lots: list[dict], rows: list[dict], *,
           generated=()) -> dict:
    from . import land, resolutions
    paths = {g.path for g in generated}
    latest: dict = {}
    for row in rows:
        latest.setdefault(frozenset((int(row["work_item_id"]), int(row["other_item_id"]))), row)
    ids = {int(one["work_item"]) for one in lots}
    wanted = [r["commit_sha"] for key, r in latest.items() if key <= ids]
    missing = set(replayer.prime(wanted)) if wanted else set()
    path = replayer.path
    run = resolutions._run
    resolutions._check(run(path, "checkout", "-q", "-f", "--detach", target_head), "checkout")
    stacked: list[int] = []
    out: dict = {}
    try:
        for one in lots:
            lot = int(one["work_item"])
            base = resolutions._check(run(path, "rev-parse", "HEAD"), "rev-parse")
            replayer._empty_cache()
            if run(path, "merge-base", "--is-ancestor", one["tip"], "HEAD").returncode == 0:
                # r6: already in the target (or a lot stacked): nothing to
                # merge, and no empty commit to stop the lots after it
                out[lot] = {"state": "contained", "files": []}
                stacked.append(lot)
                continue
            for other in stacked:
                row = latest.get(frozenset((lot, other)))
                if row and row["commit_sha"] not in missing \
                        and row["commit_sha"] in replayer.trained():
                    shutil.copytree(replayer._store(row["commit_sha"]), replayer._rr_cache(),
                                    dirs_exist_ok=True)
            merge = run(path, "-c", "rerere.enabled=false", "-c", "rerere.autoUpdate=false",
                        "merge", "--no-ff", "--no-commit", "--quiet", one["tip"])
            state, files = "clean", []
            if merge.returncode != 0:
                files = resolutions.unmerged(path)
                resolutions.exclude_generated(path, files, paths)
                resolutions._check(run(path, "-c", "rerere.enabled=true", "-c",
                                       "rerere.autoUpdate=false", "rerere"), "rerere")
                others = [f for f in files if f not in paths]
                remaining = [f for f in resolutions.rerere_remaining(path) if f not in paths] \
                    if others else []
                left = sorted(set(remaining) | set(resolutions.with_markers(path, others)))
                if not files or left:
                    run(path, "merge", "--abort")
                    run(path, "reset", "-q", "--hard", base)
                    out[lot] = {"state": "conflict", "files": left or files,
                                "error": "" if files else resolutions._err(merge)[:300]}
                    continue
                if others:
                    resolutions._check(run(path, "add", "--", *others), "add")
                    state = "replayed"
            if paths:
                made = land._regenerate(path, generated, paths)
                if not made["ok"]:
                    run(path, "merge", "--abort")
                    run(path, "reset", "-q", "--hard", base)
                    out[lot] = {"state": "generated", "files": [made["file"]],
                                "output": made["output"]}
                    continue
            resolutions._check(run(path, "-c", "rerere.enabled=false", "commit", "--quiet",
                                   "--no-verify", "-m", "queue: #%d" % lot), "commit")
            out[lot] = {"state": state, "files": [f for f in files if f not in paths]}
            stacked.append(lot)
    finally:
        run(path, "reset", "-q", "--hard")
        run(path, "clean", "-q", "-f", "-d")
        replayer._empty_cache()
    return out


# --------------------------------------------------------------------------
# a radar pass
# --------------------------------------------------------------------------

def _notified_path(cfg, project: str) -> str:
    from . import radar
    return os.path.join(radar.journal_dir(cfg), project + ".queue-notified")


def _read_notified(cfg, project: str) -> set:
    try:
        with open(_notified_path(cfg, project), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()
    return set(data) if isinstance(data, list) else set()


def _write_notified(cfg, project: str, keys: set) -> None:
    from . import radar
    directory = radar.journal_dir(cfg)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".%s.queue-notified." % project)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(sorted(keys), fh)
    os.replace(tmp, _notified_path(cfg, project))


def _wake(db, to: str | None, lot: int, text: str, data: dict) -> int | None:
    """An ordinary mail (it wakes the agent, L125 batching): the queue is
    blocked on it."""
    from . import mail
    if not to or to.startswith(work.HUMAN_PREFIX):
        return None
    try:
        return mail.send(db, work.SYSTEM_SENDER, to, text, kind="event",
                         work_item_id=str(lot), payload={"merge_queue": data})
    except Exception:  # noqa: BLE001 - retried at the next pass
        return None


def _eject(db, row: dict, reason: str, text: str, data: dict) -> dict:
    lot = int(row["work_item_id"])
    storage.of(db).work.queue_update(lot, state="ejected", reason=reason,
                                     note="merge queue of %s: ejected, %s"
                                     % (row["target"], reason), actor=work.SYSTEM_SENDER)
    sent = _wake(db, row.get("assignee"), lot, text, dict(data, lot=lot, reason=reason))
    return {"lot": lot, "to": row.get("assignee"), "kind": "ejected", "message_id": sent}


def run_pass(cfg, db, project, report: dict, *, replayer=None, cache=None) -> dict:
    """One pass of the queues of `project`, after the radar scan and the
    replay of the resolutions. Writes ranks and ejections; returns
    `{targets: {target: [{lot, rank, state, reason, stack}]}, mail: […]}`,
    kept in the radar report. Never raises for one queue: the error is
    noted."""
    from . import resolutions
    out: dict = {"targets": {}, "mail": []}
    try:
        rows = storage.of(db).work.queue()
    except Exception as exc:  # noqa: BLE001 - e.g. database not yet migrated (0058)
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        return out
    branches = {b["work_item"]: b for b in report.get("branches") or ()}
    source = source_of(project.repo)

    def ours(row: dict) -> bool:
        """A lot of this project: queued from its source (the queue holds
        every project's lots). Neither the commits a repository holds (two
        projects may share one; a checkout recreated may lack it) nor the
        scan decide it; only a row with no source, if any, falls back on the
        scan."""
        if row.get("source"):
            return normalize_source(row["source"]) == source
        return int(row["work_item_id"]) in branches

    mine = [r for r in rows if r["state"] in ("queued", "landing") and ours(r)]
    if not mine:
        return out
    notified = _read_notified(cfg, project.name)
    keys = set()
    pairs = pair_states(report)
    against = {int(a["work_item"]): a for a in report.get("against_target") or ()
               if a.get("state") == "conflict"}
    ejected: set = set()
    by_target: dict = {}
    live = []
    landing: dict = {}
    for row in mine:
        if row.get("item_state") in work.MERGED_STATES:
            storage.of(db).work.queue_update(int(row["work_item_id"]), state="landed",
                                             reason="merged",
                                             note="merge queue of %s: landed" % row["target"],
                                             actor=work.SYSTEM_SENDER)
        else:
            live.append(row)
    if live and replayer is None:
        from . import radar
        replayer = resolutions.Replayer(project.repo, radar.clone_dir(cfg, project.name),
                                        target=project.target, db=db)
    # the radar reads the agents' refs, which may be stale either way, or gone
    # (a checkout recreated): each pass fetches the branch of every queued
    # lot and its target, scanned or not, and those tips decide
    failed = None
    tips: dict = {}
    where = project.repo
    if live:
        try:
            tips, where = fetch_fresh(project.repo, [r.get("branch") for r in live]
                                      + [r["target"] for r in live], replayer)
        except QueueError as exc:
            failed = exc
            where = replayer.path

    def defer(row: dict, why: str) -> None:
        # neither ejected nor ranked on a guess: out of the order until the
        # next pass, its rank cleared so that no other lot shares it
        reason = "deferred: %s" % why
        if row.get("rank") is not None or row.get("reason") != reason:
            storage.of(db).work.queue_update(int(row["work_item_id"]), clear_rank=True,
                                             reason=reason)
        out.setdefault("deferred", []).append({"lot": int(row["work_item_id"]),
                                               "reason": why})

    for row in live:
        lot = int(row["work_item_id"])
        tip, head = tips.get(row.get("branch")), tips.get(row["target"])
        if failed is not None:
            defer(row, str(failed))
            from . import land
            note = land.lock_note(str(failed))
            if note:  # #5179 : compté par `land.lock_stuck`, comme un atterrissage
                storage.of(db).work.queue_update(lot, note=note, actor=work.SYSTEM_SENDER)
            continue
        place = "origin" if where != project.repo else "the repository"
        if tip is None:
            defer(row, "branch %s not found on %s" % (row.get("branch") or "—", place))
            continue
        if head is None:
            defer(row, "target %s not found on %s" % (row["target"], place))
            continue
        if row["state"] == "landing" and (_same(tip, row["frozen_sha"])
                                          or _same(tip, row.get("landing_sha"))):
            # being merged by ameesh (L160e2): its branch may hold the merge commit
            landing.setdefault(row["target"], []).append(row)
            continue
        if not _same(tip, row["frozen_sha"]):
            reason = "branch moved past its freeze (%s, frozen %s)" % (
                tip[:12], row["frozen_sha"][:12])
            out["mail"].append(_eject(db, row, reason, (
                "Merge queue of %s, lot #%d « %s »: out of the queue, its branch %s moved "
                "past the freeze (%s, frozen %s). A queued lot is frozen: freeze it again, "
                "have it reviewed, then ameesh work queue add %d."
                % (row["target"], lot, row.get("title") or "", row.get("branch") or "?",
                   tip[:12], row["frozen_sha"][:12], lot)),
                {"project": project.name, "target": row["target"], "kind": "ejected"}))
            continue
        clash = against.get(lot)
        # the radar tried the target its checkout had: only a conflict with
        # the target fetched now ejects; otherwise the stacked trial below,
        # on that target, decides
        ordinary = [f for f in (clash or {}).get("files", ())
                    if f not in {g.path for g in getattr(project, "generated", ())}]
        if clash and ordinary and clash.get("target") == row["target"] \
                and _same(clash.get("tip"), row["frozen_sha"]) \
                and _same(clash.get("target_head"), head):
            # no resolution is prepared against the target: the agent integrates it
            reason = "conflict with %s on %s" % (row["target"], ", ".join(
                ordinary[:6]))
            out["mail"].append(_eject(db, row, reason, (
                "Merge queue of %s, lot #%d « %s »: out of the queue, its frozen commit "
                "conflicts with %s on %s. Integrate %s into %s, freeze again, have it "
                "reviewed, then ameesh work queue add %d."
                % (row["target"], lot, row.get("title") or "", row["target"],
                   ", ".join(ordinary[:6]), row["target"], row.get("branch") or "?",
                   lot)),
                {"project": project.name, "target": row["target"], "kind": "ejected",
                 "files": ordinary}))
            ejected.add(lot)
            continue
        # stacked on the target as origin has it now, not the agents' cached one
        by_target.setdefault(row["target"], []).append(dict(row, tip=tip, target_head=head))
    try:
        resolution_rows = storage.of(db).work.resolutions(
            [int(r["work_item_id"]) for rs in list(by_target.values()) + list(landing.values())
             for r in rs]) \
            if by_target else []
    except Exception:  # noqa: BLE001 - database without 0057: no replay
        resolution_rows = []
    stacks = getattr(cache, "stacks", None) if cache is not None else None
    stacks = {} if stacks is None else stacks
    for target in sorted(set(landing) - set(by_target)):
        out["targets"][target] = [{"lot": int(r["work_item_id"]), "rank": 1, "state": "landing",
                                   "reason": r.get("reason") or "", "stack": None}
                                  for r in landing[target]]
    for target, entries in sorted(by_target.items()):
        ranked = order(entries, pairs)
        first = len(landing.get(target) or ())
        for one in ranked:
            one["rank"] += first
        rows_by_id = {int(e["work_item_id"]): e for e in entries}
        for one in ranked:
            row = rows_by_id[one["work_item"]]
            if row.get("rank") != one["rank"] or row.get("reason") != one["reason"]:
                storage.of(db).work.queue_update(one["work_item"], rank=one["rank"],
                                                 reason=one["reason"])
        heads = {e["target_head"] for e in entries}
        result: dict = {}
        if len(heads) == 1:
            commits = tuple(sorted(r["commit_sha"] for r in resolution_rows))
            # a lot being landed goes first: the lots behind it merge after it
            stacked = [{"work_item": int(r["work_item_id"]), "tip": r["frozen_sha"]}
                       for r in landing.get(target) or ()] + \
                [{"work_item": o["work_item"], "tip": rows_by_id[o["work_item"]]["tip"]}
                 for o in ranked]
            generated = tuple(getattr(project, "generated", ()))
            key = (heads.pop(), tuple((o["work_item"], o["tip"]) for o in stacked), commits,
                   generated)
            if key not in stacks:
                busy = None
                try:
                    replayer.ensure()
                    stacks[key] = stack(replayer, key[0], stacked, resolution_rows,
                                        generated=generated)
                except resolutions.CloneBusy as exc:
                    # #2428: another operation owns the clone's worktree (a
                    # landing and its gates, another pass). Nothing was read,
                    # so no lot is ejected and nothing is cached: the next
                    # pass retries the trial.
                    busy = "%s: %s" % (type(exc).__name__, exc)
                except (GitError, OSError) as exc:
                    stacks[key] = {"error": "%s: %s" % (type(exc).__name__, exc)}
                result = {"error": busy} if busy is not None else stacks[key]
            else:
                result = stacks[key]
        listed = [{"lot": int(r["work_item_id"]), "rank": 1, "state": "landing",
                   "reason": r.get("reason") or "", "stack": None}
                  for r in landing.get(target) or ()]
        for one in ranked:
            row = rows_by_id[one["work_item"]]
            trial = result.get(one["work_item"]) if isinstance(result, dict) else None
            state = "queued"
            if trial and trial["state"] == "generated":
                lot = one["work_item"]
                reason = "stacked trial: generated file %s: regeneration or check failed" % trial["files"][0]
                out["mail"].append(_eject(db, row, reason, (
                    "Merge queue of %s, lot #%d: out of the queue. %s.\n%s\n"
                    "Fix the generator or its inputs, freeze again, then ameesh work queue add %d."
                    % (target, lot, reason, trial["output"], lot)),
                    {"project": project.name, "target": target, "kind": "ejected",
                     "files": trial["files"]}))
                state = "ejected"
                ejected.add(lot)
            if trial and trial["state"] == "conflict":
                lot = one["work_item"]
                reason = "stacked trial: conflict not covered on %s" % ", ".join(
                    trial["files"][:6] or ["?"])
                out["mail"].append(_eject(db, row, reason, (
                    "Merge queue of %s, lot #%d « %s »: out of the queue. Merged after the "
                    "lots ahead of it, it conflicts on %s and no prepared resolution covers "
                    "it. Prepare it (ameesh work resolve %d --with <lot>) or integrate %s, "
                    "freeze again, then ameesh work queue add %d."
                    % (target, lot, row.get("title") or "", ", ".join(trial["files"][:6]),
                       lot, target, lot)),
                    {"project": project.name, "target": target, "kind": "ejected",
                     "files": trial["files"]}))
                state = "ejected"
                ejected.add(lot)
            listed.append({"lot": one["work_item"], "rank": one["rank"], "state": state,
                           "reason": one["reason"], "stack": trial})
        for one in ranked:
            for other, resolver in one["waits"]:
                who = resolver or one["work_item"]
                if ejected & {one["work_item"], other, who}:
                    # out of the queue in this pass: nobody waits on it, and an
                    # ejected resolver already has its ejection mail
                    continue
                key = "wait:%d-%d" % (other, one["work_item"])
                keys.add(key)
                if key in notified:
                    continue
                holder = rows_by_id.get(who) or {}
                to = holder.get("assignee") or (branches.get(who) or {}).get("assignee")
                sent = _wake(db, to, who, (
                    "Merge queue of %s: lot #%d waits behind lot #%d, and their conflict "
                    "is not prepared. It now blocks the queue: ameesh work resolve %d "
                    "--with %d." % (target, one["work_item"], other, who,
                                    other if who == one["work_item"] else one["work_item"])),
                    {"project": project.name, "target": target, "kind": "wait",
                     "lots": [other, one["work_item"]]})
                out["mail"].append({"lot": who, "to": to, "kind": "wait",
                                    "message_id": sent})
        out["targets"][target] = listed
        if isinstance(result, dict) and result.get("error"):
            out["error"] = result["error"]
    if keys != notified:
        _write_notified(cfg, project.name, keys)
    return out


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def add_parser(work_sub, func) -> None:
    parser = work_sub.add_parser(
        "queue", help="merge queue per target (L160e): a ready lot enters it, ameesh orders "
                      "it and replays it stacked")
    sub = parser.add_subparsers(dest="queue_command", required=True)
    add = sub.add_parser("add", help="put a frozen, reviewed lot into its target's queue")
    add.add_argument("id", type=int)
    add.add_argument("--repo", default=None,
                     help="the project repository (default: the current git repository)")
    rm = sub.add_parser("remove", help="take a lot out of the queue")
    rm.add_argument("id", type=int)
    rm.add_argument("-m", "--message", default="", help="why")
    show = sub.add_parser("show", help="the queues, in merge order")
    show.add_argument("--target", default=None)
    for p in (add, rm, show):
        p.add_argument("--json", action="store_true")
        p.add_argument("--actor", default="",
                       help="default: the identity bound to the session (AGENT_MAIL_NAME)")
    parser.set_defaults(func=func)


def show_lines(rows: list[dict], classes: dict | None = None) -> list[str]:
    """Les lignes de `ameesh work queue` ; `classes` (L539) ajoute la classe de
    revue de chaque lot quand elle est connue ({lot: « light »…})."""
    out = []
    target = None
    for r in rows:
        if r["target"] != target:
            target = r["target"]
            out.append("queue of %s:" % target)
        klass = (classes or {}).get(int(r["work_item_id"]))
        out.append("  %-3s %-8s #%d « %s » (%s, frozen %s%s)%s" % (
            r["rank"] if r.get("rank") else "—", r["state"], int(r["work_item_id"]),
            r.get("title") or "", r.get("assignee") or "unassigned", r["frozen_sha"][:12],
            ", class %s" % klass if klass else "",
            " : %s" % r["reason"] if r.get("reason") else ""))
    return out


def radar_project(cfg, repo: str):
    """The radar project (`radar.projects`) of the repository `repo`, if any:
    its private clone receives the fetches of `readiness`."""
    from . import radar, migrations
    try:
        projects = list(radar.settings_of(cfg).projects.values())
    except radar.SettingsError as exc:
        raise QueueError("cannot read repository landing conditions: %s" % exc) from exc
    real = os.path.realpath(repo)
    identities = {p.name: {migrations.repo_key(p.github),
                           migrations.repo_key(migrations.origin_repo(p.repo))} - {""}
                  for p in projects}
    slugs = {migrations.repo_key(migrations.origin_repo(repo))} - {""}
    # A checkout without origin still has its configured repository identity.
    # An exact path contributes matches; it never suppresses another project's.
    for p in projects:
        if os.path.realpath(p.repo) == real:
            slugs.update(identities[p.name])
    matching = [p for p in projects if os.path.realpath(p.repo) == real or
                slugs & identities[p.name]]
    if len(matching) > 1:
        raise QueueError("multiple radar projects match this repository; landing conditions are ambiguous")
    return matching[0] if matching else None


def declared_projects(cfg) -> dict:
    """Declared radar projects; an agent without a radar profile keeps its
    disposable clone, while an incompatible declared profile is refused.
    Restores the qualified #1467 union d9a80c1a7 after #2120 transport.
    """
    from . import radar
    try:
        return dict(radar.settings_of(cfg).projects)
    except radar.SettingsError as exc:
        raise QueueError("cannot read repository landing conditions: %s" % exc) from exc


def writer_project(cfg, repo: str, app: str = "", project=None):
    """The project identity a single-lot writer shares with the train (#1703).

    `land.lock_key` keys a target lease on `project.name`. A packet train holds
    the key of the **declared** radar project of its repository, so the direct
    merge path (`merge_pr.direct_merge`) must resolve that same identity,
    never a parallel one: the declared project comes first, whatever the
    admitted lot's `app` says. Without a declared project no train can run on
    this host, and the lot's `app` is then the explicit identity already
    carried by the queue — no repository key is invented here (#1680). An
    empty app refuses: an anonymous writer would open a key space of its own.
    """
    if project is None:
        project = radar_project(cfg, repo)
    if project is not None:
        return project
    name = (app or "").strip()
    if not name:
        raise QueueError("cannot reserve an undeclared target project")
    return SimpleNamespace(name=name)


def working_clone(cfg, repo: str, project) -> tuple:
    """Le clone où `queue add` lit les branches fraîches, et le temporaire à
    retirer (`None` quand c'est le clone du radar).

    Un passage de radar a son clone privé (`radar.clone_dir`) ; un poste agent
    ou pilote n'en a pas, et la file doit pourtant s'ouvrir de là (§2 : le
    pilote met un lot en file depuis son poste). On prête alors un clone
    jetable du dépôt local (`--shared`), le temps de la commande : la lecture
    reste fraîche (`fetch_fresh`), le dépôt des agents n'est jamais écrit, et
    rien ne survit à la commande."""
    from . import radar, resolutions
    if project is not None:
        return resolutions.Replayer(repo, radar.clone_dir(cfg, project.name),
                                    target=project.target), None
    scratch = tempfile.mkdtemp(prefix="ameesh-queue-")
    return resolutions.Replayer(repo, os.path.join(scratch, "clone")), scratch


def run(cfg, db, args) -> int:
    import sys
    from . import radar, resolutions
    try:
        if args.queue_command == "add":
            repo = resolutions.repo_root(args.repo)
            project = radar_project(cfg, repo)
            if (project is None and declared_projects(cfg)
                    and plan_git._out(repo, "remote", "get-url", "origin")):
                raise QueueError(
                    "no radar project matches repository %s (source %s) in the loaded "
                    "configuration: select the profile declaring radar.projects for this "
                    "repository, including its land/protected conditions; --repo selects "
                    "a local checkout, not a configuration profile" % (repo, source_of(repo)))
            # #1761 (main, 15/10) : sans projet radar déclaré, `working_clone`
            # prête un clone jetable et la file s'ouvre depuis un poste agent ou
            # pilote. #1467 refusait ce cas (branche partie avant #1761) :
            # l'union garde le comportement fusionné de main.
            clone, scratch = working_clone(cfg, repo, project)
            try:
                row = enter(db, args.id, repo=repo, actor=args.actor, project=project,
                            clone=clone, cfg=cfg)
            finally:
                if scratch:
                    shutil.rmtree(scratch, ignore_errors=True)
            print(json.dumps(row, ensure_ascii=False, indent=2, default=str) if args.json else
                  "lot #%d queued for %s at %s, class %s%s; the radar orders the queue at "
                  "its next pass" % (args.id, row["target"], row["frozen_sha"][:12],
                                     row.get("class") or "?",
                                     " (%s)" % row["vehicle"] if row.get("vehicle")
                                     else ""))
            return 0
        if args.queue_command == "remove":
            train = live_train(cfg, db, args.id)
            done = leave(db, args.id, actor=args.actor, reason=args.message)
            if done and train:
                print("warning: lot #%d is a member of the live train %s (%s, phase %s): "
                      "this take-out invalidates it; it lands nothing and frees the target "
                      "once its CI ends" % (args.id, train["stamp"] or "?", train["key"],
                                            train["phase"]), file=sys.stderr)
            print(json.dumps(dict({"lot": args.id, "removed": done},
                                  **({"train": train} if done and train else {})))
                  if args.json else
                  "lot #%d %s" % (args.id, "taken out of the queue" if done
                                  else "was not queued"))
            return 0
        rows = storage.of(db).work.queue(args.target)
        # L539 : la classe de chaque lot de la file (lecture seule, best effort)
        from . import lot_class
        classes = {}
        for r in rows:
            info = lot_class.of_lot(db, int(r["work_item_id"]), cfg=cfg)
            classes[int(r["work_item_id"])] = info.get("class")
            r["class"] = info.get("class")
            r["class_why"] = info.get("why")
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        else:
            print("\n".join(show_lines(rows, classes)) if rows else "no lot queued")
        return 0
    except (QueueError, GitError, plan_git.MergeProbeError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
