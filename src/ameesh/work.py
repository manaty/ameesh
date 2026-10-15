# SPDX-License-Identifier: AGPL-3.0-only
"""work_items — la machine à états des lots, reprise d'un orchestrateur de
tickets existant (spec §5 point 4).

    intake → build → qa → merged → promoted

plus `blocked` et `waiting_human`, et l'état terminal `closed` (abandonné ou
remplacé par un autre lot : `close`, L29). La boucle `qa → build` est bornée à deux
allers-retours (comme dans le design d'origine) : au-delà, il faut bloquer le
lot ou attendre une décision humaine, sinon la boucle coûte des tokens sans fin.

Chaque transition laisse une ligne dans `work_item_events` (qui, quand,
pourquoi) : c'est le ledger de reprise après un redémarrage.

Plan de travail (L29) : un lot peut être rattaché à une fiche `WorkPackage`
du canon (`package_id`, et son parent `package_parent`) ; une fusion
constatée (porte `git-merge`, ou `ameesh work sync-github`) le ferme par
`close_merged`, idempotent et sans jamais rouvrir un lot fermé.

Correctif du suivi des lots (2026-10-11) : une fusion faite hors PR se
déclare en une fois (`merged`, depuis tout état ouvert) ; un `promoted` posé
par erreur se corrige (`move(..., correct=…)`, tracé, réservé aux humains et
aux orchestrateurs ou agents de conception, `corrector`) ; l'acteur d'une
commande `work` est l'identité liée de la session, jamais vide
(`resolve_actor`).

L233: a lot enters `waiting_human` only while an owner decision request is
open for it (`ameesh decide ask` moves it there); `move` refuses otherwise.

Le SQL est dans le stockage (`storage.of(db).work`, spec §10).
"""
from __future__ import annotations

import os
import re
import time
from typing import Sequence

from . import storage
from .db import Db, DbError, is_unique_violation
from .storage.postgres import work as _pg

STATES = ("intake", "build", "qa", "merged", "promoted", "blocked", "waiting_human",
          "closed")
TERMINAL = ("promoted", "closed")
#: déjà fusionnés : une fusion constatée n'y change rien
MERGED_STATES = ("merged", "promoted")
#: raisons d'une fermeture explicite (`ameesh work close`)
CLOSE_REASONS = ("abandoned", "superseded")
MAX_QA_LOOPS = 2
FORWARD = {
    "intake": ("build",),
    "build": ("qa",),
    "qa": ("merged", "build"),
    "merged": ("promoted",),
    "promoted": (),
    "closed": (),
}

#: transitions autorisées, attentes comprises
TRANSITIONS = {
    "intake": {"build", "blocked", "waiting_human"},
    "build": {"qa", "blocked", "waiting_human"},
    "qa": {"merged", "build", "blocked", "waiting_human"},
    "merged": {"promoted", "blocked", "waiting_human"},
    "promoted": set(),
    "closed": set(),
    "blocked": {"intake", "build", "qa", "merged", "waiting_human"},
    "waiting_human": {"intake", "build", "qa", "merged", "blocked"},
}
WAITING = {"blocked", "waiting_human"}
#: corrections d'un état posé par erreur (`move(..., correct=…)`) : (depuis,
#: vers). Un `promoted` à tort revient en `merged` ; ensuite, les transitions
#: habituelles (merged → blocked → …) s'appliquent.
CORRECTIONS = {("promoted", "merged")}
#: rôles d'une fiche Agent du canon admis à corriger un état (avec les humains)
CORRECTOR_ROLES = ("orchestrateur", "orchestrator", "conception", "design")
#: acteur d'un geste dont l'auteur n'est pas connu : jamais un acteur vide
UNKNOWN_ACTOR = "inconnu"
#: un commit de fusion déclaré (`merged --sha`)
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
#: priorités d'un lot (L130, comme la file d'amélioration L119) : 1 haute,
#: 2 normale, 3 basse ; sans priorité, le lot n'en a pas (NULL)
PRIORITIES = (1, 2, 3)
PRIORITY_LABELS = {1: "high", 2: "normal", 3: "low"}
#: L157b : taille déclarée du changement d'un lot (petit, moyen, gros)
SIZES = ("s", "m", "l")
#: L233: a lot enters `waiting_human` only while an owner decision request
#: (`ameesh decide ask`) is open for it; a decision written in prose in mail
#: never counts (the owner was shown lots "waiting for them" with no question)
WAITING_WITHOUT_QUESTION = (
    "refused: lot %(id)s enters waiting_human only through an open owner decision "
    "request — ameesh decide ask --lot %(id)s --question \"…\" --option a=\"…\" (the "
    "lot moves to waiting_human by itself); a decision written in a mail never counts")

#: colonnes rendues pour un lot (pilote Postgres ; alias de compatibilité)
ITEM_COLUMNS = _pg.ITEM_COLUMNS


class WorkError(RuntimeError):
    """Transition ou donnée refusée — message toujours actionnable."""


# --------------------------------------------------------------------------
# acteur d'une commande `work` (correctif du 2026-10-11)
# --------------------------------------------------------------------------

def resolve_actor(cfg, db, explicit: str | None = None) -> tuple[str, str | None]:
    """L'acteur d'une commande `work`, jamais vide : `--actor` s'il est donné,
    sinon l'identité liée de la session (`AGENT_MAIL_NAME`, posée par
    l'exécuteur, ou liaison de session : `identity.resolve_binding`), sinon
    « inconnu ». Rend `(acteur, avertissement ou None)`."""
    from . import identity

    text = (explicit or "").strip()
    if text:
        return text, None
    binding = identity.resolve_binding(cfg, db)
    if binding.name:
        if binding.ok:
            return binding.name, None
        return binding.name, ("identity %s not bound (%s): used as actor"
                              % (binding.name, binding.reason or "unknown reason"))
    return UNKNOWN_ACTOR, ("unknown actor: no identity bound to this session "
                           "(AGENT_MAIL_NAME, ameesh mail bind); the lot journal will say "
                           "\"%s\"; pass --actor <agent|human:id>" % UNKNOWN_ACTOR)


def corrector(cfg, db, explicit: str | None = None, *, history: bool = True) -> str:
    """Qui corrige un état posé par erreur (`work move … --correct`) ; WorkError
    si la correction lui est refusée.

    Réservée aux humains et aux orchestrateurs ou agents de conception. Une
    session qui porte une identité d'agent (`identity.resolve_binding`) corrige
    en son nom, et seulement si cet agent est orchestrateur ou de conception
    (`assignments.is_orchestrator` avec les rôles `CORRECTOR_ROLES` : rôle de sa
    fiche Agent au canon, `AMEESH_ALERT_ORCHESTRATORS`, ou lots confiés
    récemment) ; elle ne peut pas corriger au nom d'un autre. Une session sans
    identité d'agent (un humain à son terminal) dit qui corrige : `--actor
    human:<id>`, un humain connu, ou un orchestrateur. `history=False` (#740) :
    les lots confiés récemment ne comptent pas, seul un rôle déclaré compte."""
    from . import assignments, identity, registry

    text = (explicit or "").strip()
    binding = identity.resolve_binding(cfg, db)
    # A rejected runner/session identity must never fall back to the human
    # terminal path, including a malformed AGENT_MAIL_NAME (no returned name).
    if not binding.ok and (binding.name or binding.source != "none"
                           or os.environ.get("AGENT_MAIL_NAME")):
        raise WorkError("correction refused: invalid session identity (%s)"
                        % (binding.reason or "unbound identity"))
    refus = ("correction refused: %s is neither a human, an orchestrator nor a design agent "
             "(orchestrator or design role on its Agent card in the canon, "
             "AMEESH_ALERT_ORCHESTRATORS); ask the orchestrator or the owner")
    if binding.name:
        if text and text != binding.name:
            raise WorkError("correction refused: this session belongs to %s, it does not "
                            "correct on behalf of %s" % (binding.name, text))
        if assignments.is_orchestrator(cfg, db, binding.name, roles=CORRECTOR_ROLES,
                                       history=history):
            return binding.name
        raise WorkError(refus % binding.name)
    if not text:
        raise WorkError("correction: say who corrects (--actor human:<id>)")
    if text.startswith(HUMAN_PREFIX):
        return text
    human = known_human(db, text, cfg)
    if human:
        return human
    if registry.get(db, text) is not None \
            and assignments.is_orchestrator(cfg, db, text, roles=CORRECTOR_ROLES,
                                            history=history):
        return text
    raise WorkError(refus % text)


def pin_authority(cfg, db, actor: str | None = None) -> str | None:
    """#740: who may protect a date from the schedule (`work plan`, `work
    schedule --pin`): the actor `corrector` accepts on a declared role (a
    human, or an orchestrator or design role in the canon or
    AMEESH_ALERT_ORCHESTRATORS; having assigned lots is not enough), or None."""
    try:
        return corrector(cfg, db, actor, history=False)
    except WorkError:
        return None


def add(
    db: Db,
    *,
    title: str,
    type: str = "evolution",  # noqa: A002 - nom de colonne de l'orchestrateur d'origine
    source: str = "",
    app: str = "",
    body: str = "",
    issue_ref: str | None = None,
    workstream: str | None = None,
    assignee: str | None = None,
    budget_usd: float | None = None,
    actor: str = "",
    package: str | None = None,
    externe: bool = False,
    cfg=None,
    branch: str | None = None,
    branch_target: str | None = None,
    priority: int | None = None,
    estimate=None,
    estimate_source: str | None = None,
    size: str | None = None,
    note: str = "",
    parent_lot: int | None = None,
    mark_item_id: int | None = None,
    mark_note: str = "",
) -> dict:
    """Crée un lot en `intake`. Un assigné passe par l'attribution gardée
    (`check_assignee`, L37) : un agent non réveillable est refusé (WorkError).
    L118 : `branch` (et `branch_target`) nomment la branche du lot, dont la
    fusion le fermera (`plan_git.sync_branches`). L130 : `priority` (1 haute,
    2 normale, 3 basse, comme la file d'amélioration), pour enregistrer une
    demande d'humain avec sa priorité. L317c : un bug de priorité 1 ouvert par
    un humain ou son chat demande une analyse profonde à l'auditeur
    (`deep_analysis.on_lot_created`, rendu sous la clé `deep_analysis`).

    L157 : `estimate` (« 2h », « 90m », ou des minutes) est la durée estimée,
    posée avec sa source et son auteur (`actor`). La règle « obligatoire pour
    un agent » est appliquée par les points d'entrée (`estimates.check`).
    L157b : `size` (`s`, `m`, `l`) est la taille déclarée du changement, une
    clé de la table de référence des durées.
    #506: `note` is appended to the creation event (a project introduced
    with --new-project). L539 : `mark_item_id` et `mark_note` écrivent la
    marque d'idempotence du lot de relecture au journal de ce lot, dans la
    même transaction que la création (`{lot}` y vaut l'identifiant créé).

    #1193: `parent_lot` is the explicit parent of a suite lot. The blocking
    edge is written **in the same transaction** as the creation (parent still
    open), traced in the journal if the parent is already finished; an unknown
    parent is refused before any creation. No number is ever guessed from the
    title or the body."""
    minutes = None
    if estimate not in (None, ""):
        from . import estimates
        minutes = estimates.parse(estimate)
    if type not in ("bug", "evolution"):
        from .modules.storage import ModuleStore
        if not ModuleStore(db).admits(app or workstream or "", type):
            raise WorkError("unknown type %r for project %r: enable its module in the canon"
                            % (type, app or workstream or ""))
    if size not in (None, "") and size not in SIZES:
        raise WorkError("unknown size: %r (s, m or l)" % (size,))
    if not (title or "").strip():
        raise WorkError("title required")
    if priority is not None and priority not in PRIORITIES:
        raise WorkError("priority 1 (high), 2 (normal) or 3 (low): %r" % (priority,))
    if parent_lot is not None:
        try:
            parent_lot = int(parent_lot)
        except (TypeError, ValueError):
            raise WorkError("--parent-lot: a lot number, not %r" % (parent_lot,)) from None
        if parent_lot <= 0:
            raise WorkError("--parent-lot: a positive lot number")
        if get(db, parent_lot) is None:
            raise WorkError("parent lot #%d not found" % parent_lot)
    if assignee:
        # L46 : l'assigné enregistré est celui que la garde a normalisé
        assignee = check_assignee(db, assignee, externe=externe, cfg=cfg)["assignee"]
    elif externe:
        raise WorkError("--externe without assignee: nothing to force")
    fiche = _package(db, package) if package else None
    branch, branch_target = _branch_args(branch, branch_target)
    item = storage.of(db).work.add(
        type=type, source=source, app=app, title=title.strip(), body=body,
        issue_ref=issue_ref, workstream=workstream, assignee=assignee,
        budget_usd=budget_usd, note="created" + (" (plan: %s)" % package if package else "")
        + ("; estimate: %s" % span(minutes * 60) if minutes else "")
        + ("; %s" % note if note else ""),
        actor=actor, package_id=fiche["id"] if fiche else None,
        package_parent=fiche.get("parent") if fiche else None,
        branch=branch, branch_target=branch_target, priority=priority,
        estimate_minutes=minutes,
        estimate_source=(" ".join(str(estimate_source or "").split()) or None)
        if minutes else None, change_size=size or None,
        mark_item_id=mark_item_id, mark_note=mark_note, parent_lot=parent_lot)
    # L317c : un bug de priorité 1 ouvert par un humain (ou son chat) réveille
    # l'auditeur pour une analyse profonde, tout de suite
    from . import deep_analysis
    analysis = deep_analysis.on_lot_created(db, item, actor=actor, cfg=cfg)
    if analysis is not None:
        item["deep_analysis"] = analysis
    return item


# --------------------------------------------------------------------------
# branche d'un lot (L118)
# --------------------------------------------------------------------------

def _branch_args(branch, target) -> tuple:
    """(branche, cible) validées, ou (None, None) ; une cible sans branche
    est refusée."""
    from . import plan_git

    branch = (branch or "").strip() or None
    target = (target or "").strip() or None
    if target and not branch:
        raise WorkError("a target without a branch: also pass --branch")
    try:
        return (plan_git.check_ref(branch, "branch") if branch else None,
                plan_git.check_ref(target, "target") if target else None)
    except ValueError as exc:
        raise WorkError(str(exc)) from exc


def set_branch(db: Db, item_id: int, branch: str, target: str | None = None, *,
               actor: str = "", source: str = "") -> dict:
    """L118 : la branche (et la cible) d'un lot ouvert ; journalisée. Rend
    le lot. Une branche identique (et la même cible) ne réécrit rien."""
    branch, target = _branch_args(branch, target)
    if not branch:
        raise WorkError("empty branch")
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if item.get("branch") == branch and (target is None or item.get("branch_target") == target):
        return item
    target = target if target is not None else (
        item.get("branch_target") if item.get("branch") == branch else None)
    note = "branch %s%s%s (before: %s)" % (
        branch, " to %s" % target if target else "",
        " — %s" % source if source else "", item.get("branch") or "none")
    row = storage.of(db).work.set_branch(item_id, branch, target, note=note, actor=actor)
    if row is None:
        raise WorkError("lot %s already %s: branch not set" % (item_id, item["state"]))
    return row


# --------------------------------------------------------------------------
# numéro de migration réservé (L122)
# --------------------------------------------------------------------------

#: le dépôt d'un lot, lu dans son issue : `owner/repo#N`
_ISSUE_REPO_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#[0-9]+$")


def lot_repository(item: dict, cfg=None) -> str:
    """#443 : le dépôt `owner/repo` d'un lot, celui de son issue, sinon celui
    que la configuration de l'hôte donne à son projet (`github.projects`)."""
    from .migrations import repo_key

    match = _ISSUE_REPO_RE.match((item.get("issue_ref") or "").strip())
    if match:
        return repo_key(match.group(1))
    if cfg is not None and (item.get("app") or "").strip():
        from . import plan_github

        try:
            repo = plan_github.settings_of(cfg).repo_for(item["app"])
        except plan_github.SettingsError:
            repo = None
        if repo:
            return repo_key(repo)
    raise WorkError("lot #%s has no repository: link its issue first "
                    "(`ameesh work link %s owner/repo#N`)" % (item["id"], item["id"]))


def reserve_migration(db: Db, item_id: int, number: int | None = None, *,
                      release: bool = False, repo: str | None = None,
                      actor: str = "", cfg=None) -> dict:
    """L122, #443 : réserve au lot le numéro de sa migration, avant d'écrire
    le fichier, dans le dépôt du lot (`lot_repository`) : le dossier et la
    numérotation sont les siens, jamais ceux d'ameesh pour un autre dépôt.
    `repo` : un clone local de ce dépôt (défaut : celui que l'hôte déclare,
    sinon le dépôt git courant) ; son `origin` doit être le dépôt du lot.
    Sans `number` : le prochain libre, au-delà des numéros du dépôt (arbre,
    toutes ses branches) et de ceux déjà réservés pour ce dépôt. `number` est
    refusé s'il existe déjà dans le dépôt ou s'il est réservé par un autre lot
    (index unique (dépôt, numéro) : la base tranche, même entre deux
    réservations simultanées). Un lot qui a déjà sa réservation la garde
    (idempotent). `release` la libère. Rend le lot, avec `migration_file`."""
    from . import migrations

    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if item["state"] in ("merged", "promoted", "closed"):
        raise WorkError("lot %s already %s: no reservation" % (item_id, item["state"]))
    current = item.get("migration")
    if release:
        if current is None:
            return item
        layout = migration_layout(item, cfg)
        row = storage.of(db).work.reserve_migration(
            item_id, item.get("migration_repo"), None, 0, actor=actor, release=True,
            note="migration %s released (%s)" % (layout.label(current), layout.repo))
        if row is None:
            raise WorkError("lot %s is no longer open: no reservation" % item_id)
        return row
    if number is not None and not 1 <= int(number) <= 9999:
        raise WorkError("migration number outside 0001-9999: %s" % number)
    if current is not None and (number is None or current == int(number)):
        return _with_file(item, cfg)
    slug = lot_repository(item, cfg)
    layout = migrations.layout_for(slug, cfg)
    if layout is None:
        raise WorkError("repository %s: migrations directory unknown (declare it in the host "
                        "configuration: radar.projects.<project> with github %s and "
                        "migrations <dir>)" % (slug, slug))
    checkout = _checkout(slug, repo, cfg)
    present = migrations.repo_numbers(checkout, layout, skip_branch=item.get("branch"))
    if number is None:
        floor = max(present or [0]) + 1
        note = "migration %%(number)0%dd reserved (%s)" % (layout.width, slug)
    else:
        number = int(number)
        if number in present:
            raise WorkError("migration %s already in %s (%s): choose another number" % (
                layout.label(number), slug, ", ".join(sorted(present[number])[:3])))
        floor = 0
        note = "migration %s reserved (%s; before: %s)" % (
            layout.label(number), slug,
            layout.label(current) if current is not None else "none")
    try:
        row = storage.of(db).work.reserve_migration(item_id, slug, number, floor, note=note,
                                                    actor=actor)
    except storage.ReservationContention as exc:
        # #478 : budget de réessais épuisé sous la rafale — message lisible,
        # jamais la DbError brute de l'index unique
        raise WorkError("reservation contention, retry") from exc
    except DbError as exc:
        # une autre erreur qui nomme l'index (permission refusée) n'est pas
        # une réservation concurrente : elle remonte telle quelle (codex #478 r1)
        if number is None or not is_unique_violation(exc, _pg.MIGRATION_INDEX):
            raise
        holder = storage.of(db).work.migration_holder(number, slug)
        raise WorkError("migration %s of %s already reserved by lot #%s (%s)" % (
            layout.label(number), slug, holder["id"] if holder else "?",
            holder["title"] if holder else "concurrent reservation")) from exc
    if row is None:
        raise WorkError("lot %s is no longer open: no reservation" % item_id)
    return _with_file(row, cfg)


def migration_layout(item: dict, cfg=None):
    """La disposition des migrations du dépôt réservé (défaut : 4 chiffres)."""
    from . import migrations

    slug = item.get("migration_repo") or ""
    return migrations.layout_for(slug, cfg) or migrations.Layout(slug, "", 4)


def _with_file(item: dict, cfg=None) -> dict:
    if item.get("migration") is None:
        return item
    layout = migration_layout(item, cfg)
    return dict(item, migration_label=layout.label(item["migration"]),
                migration_file=layout.file(item["migration"]) if layout.directory else None)


def _checkout(slug: str, path: str | None, cfg=None) -> str:
    """Le clone local du dépôt `slug` à lire, vérifié par son `origin`."""
    from . import migrations

    where = path or migrations.checkout_for(slug, cfg) or os.getcwd()
    top = migrations.toplevel(where)
    if not top:
        raise WorkError("%s is not a git repository: pass --repo <clone of %s>" % (where, slug))
    origin = migrations.origin_repo(top)
    if origin != slug:
        raise WorkError("%s is a clone of %s, not of %s (the lot's repository): "
                        "pass --repo <clone of %s>" % (top, origin or "no GitHub origin",
                                                       slug, slug))
    return top


def open_lots(db: Db, assignee: str) -> list[dict]:
    """L118 : les lots ouverts d'un assigné (ni livrés, ni fermés)."""
    return storage.of(db).work.open_for(assignee)


# --------------------------------------------------------------------------
# attribution gardée (L37, règle 2 de la décision 0030)
# --------------------------------------------------------------------------

#: préfixe d'un assigné humain : un lot qui attend un humain n'a rien à réveiller
HUMAN_PREFIX = "human:"


def configured_canons(cfg) -> list:
    """Les canons configurés, chargés (L46, pour `known_human`) ; [] sans
    canon ou si l'un d'eux est illisible. Jamais d'exception."""
    if cfg is None:
        return []
    try:
        from . import canon as canon_mod
        if not canon_mod.configured(cfg):
            return []
        return list(canon_mod.from_config_all(cfg))
    except Exception:
        return []


def known_human(db: Db, name: str, cfg=None, *, canons=None) -> str | None:
    """L46 : `human:<id>` si le nom NU `name` désigne un humain connu, sinon
    None. Connu : humain déclaré de la configuration (`AMEESH_HUMANS`, dont le
    propriétaire), fiche `Member` d'un canon configuré (résolution stricte du
    canon : un Member unique, pas homonyme d'un agent), ou humain déjà résolu
    en base (responsable d'un agent ou d'un paquet du canon). Jamais
    d'exception : un canon illisible ne compte simplement pas. `canons` :
    une fonction qui rend les canons chargés (cache de l'appelant), sinon
    ils sont lus ici, au besoin."""
    name = (name or "").strip()
    if not name or ":" in name:
        return None
    human = HUMAN_PREFIX + name
    cfg = cfg if cfg is not None else getattr(db, "cfg", None)
    if cfg is not None and name in (getattr(cfg, "human_names", None) or ()):
        return human
    try:
        if storage.of(db).agents.human_known(human):
            return human
    except Exception:
        pass
    for canon in (canons() if canons is not None else configured_canons(cfg)):
        try:
            if canon.resolve_human(human):
                return human
        except Exception:
            continue
    return None


#: L46 : ce qu'il faut faire d'un nom refusé
_ASSIGNEE_HINT = ("register the agent (agent-runner register <name> <harness> …) so "
                  "ameesh can wake it, assign to a human (human:<id>), or, for "
                  "an external session with a human owner, add --externe")


def check_assignee(db: Db, assignee: str, *, externe: bool = False, cfg=None) -> dict:
    """L37 (0030, règle 2) : un lot ne va qu'à un agent qu'ameesh sait réveiller.

    Rend `{assignee, externe, responsible, warning}` ou lève WorkError avec la
    raison. Un humain (`human:…`) est toujours accepté. Un agent doit être
    réveillable (`registry.wakeable` : connu, `execute`, admis, responsable
    s'il est requis). `externe=True` (option `--externe`) force l'attribution
    à un agent EXTERNE, seulement s'il a un humain responsable : c'est lui qui
    portera l'attente et les alertes du lot. Un nom inconnu est toujours
    refusé. Un agent arrêté reste accepté, avec un avertissement (`warning`).

    L46 : un nom NU inconnu du registre mais qui désigne un humain connu
    (`known_human` : Member d'un canon, humain déclaré) est normalisé en
    `human:<id>`, avec un avertissement — `assignee` rendu est alors le nom
    normalisé, à enregistrer. Un canon momentanément invalide ne bloque pas
    l'attribution (avertissement seulement). Les refus disent quoi faire.
    """
    from . import registry

    name = (assignee or "").strip()
    if not name:
        raise WorkError("empty assignee")
    if name.startswith(HUMAN_PREFIX):
        if externe:
            raise WorkError("--externe targets an external agent, not a human (%s)" % name)
        return {"assignee": name, "externe": False, "responsible": None, "warning": None}
    cfg = cfg if cfg is not None else getattr(db, "cfg", None)
    row = registry.get(db, name)
    if row is None:
        human = known_human(db, name, cfg)
        if human and not externe:
            return {"assignee": human, "externe": False, "responsible": None,
                    "warning": "\"%s\" is not an agent but a known human: lot "
                               "assigned to %s" % (name, human)}
        raise WorkError("assignment refused: %s is not in the registry (unknown "
                        "agent): nobody would work on this lot; %s"
                        % (name, _ASSIGNEE_HINT))
    mode = row.get("mode") or "execute"
    responsible = (row.get("responsible") or "").strip() or None
    if externe:
        if mode != "externe":
            raise WorkError("--externe refused: %s is a %s agent (ameesh can wake "
                            "it: assign it without --externe)" % (name, mode))
        if not responsible:
            raise WorkError("--externe refused: external agent %s has no human owner "
                            "to carry the lot (0030); set one first "
                            "(canon, or ameesh set)" % name)
        return {"assignee": name, "externe": True, "responsible": responsible,
                "warning": None}
    ok, why, note = registry.wake_status(db, cfg, name)
    if not ok:
        if mode == "externe":
            hint = (" — its owner can force it with --externe" if responsible
                    else " — set its human owner first, then --externe")
        else:
            hint = (" — fix its registration or admission (agent-runner register "
                    "…, canon sync), or assign to a human (human:<id>)")
        raise WorkError("assignment refused: %s%s" % (why, hint))
    warnings = []
    if row.get("status") == "stopped":
        warnings.append("%s is stopped (%s): the lot will wait for it to resume"
                        % (name, row.get("stop_reason") or "unknown reason"))
    if note:
        warnings.append(note)
    return {"assignee": name, "externe": False, "responsible": responsible,
            "warning": "; ".join(warnings) or None}


def assign(db: Db, item_id: int, assignee: str, *, externe: bool = False, actor: str = "",
           cfg=None, branch: str | None = None, branch_target: str | None = None) -> dict:
    """Réassigne un lot ouvert (L37, `ameesh work assign`), sous la même garde
    que la création (`check_assignee`). Une ligne de journal dit l'ancien et
    le nouvel assigné. Rend `{item, check, previous}`.

    L118 : `branch` (et `branch_target`) posent aussi la branche du lot ; avec
    une branche, un lot déjà assigné à cet agent n'est pas une erreur (seule
    la branche est posée)."""
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if item["state"] in MERGED_STATES or item["state"] == "closed":
        raise WorkError("lot %s already %s: nothing to reassign" % (item_id, item["state"]))
    previous = item.get("assignee")
    _branch_args(branch, branch_target)
    check = check_assignee(db, assignee, externe=externe, cfg=cfg)
    if previous == check["assignee"]:
        if branch:
            row = set_branch(db, item_id, branch, branch_target, actor=actor)
            return {"item": row, "check": check, "previous": previous}
        raise WorkError("lot %s already assigned to %s" % (item_id, previous))
    note = "assigned to %s%s (before: %s)%s" % (
        check["assignee"],
        " — external session, owner %s" % check["responsible"] if check["externe"] else "",
        previous or "nobody",
        # L40 (0030) : la réassignation efface la délégation en cours
        "; delegation from %s cancelled" % item["delegated_by"]
        if item.get("delegated_by") and item.get("due_ts") else "")
    row = storage.of(db).work.assign(item_id, check["assignee"], current=previous, note=note,
                                     actor=actor)
    if row is None:
        raise WorkError("lot %s changed in the meantime: retry" % item_id)
    if branch:
        row = set_branch(db, item_id, branch, branch_target, actor=actor)
    return {"item": row, "check": check, "previous": previous}


# --------------------------------------------------------------------------
# délégation à échéance (L40, point 5 de la décision 0030)
# --------------------------------------------------------------------------

#: expéditeur des événements qu'ameesh écrit lui-même (échéance d'une délégation)
SYSTEM_SENDER = "ameesh"
#: issues d'une délégation au registre `work_item_delegations`
DELEGATION_OUTCOMES = ("soldee", "rendue", "remplacee", "annulee")


def span(seconds) -> str:
    """Une durée lisible : « 45 s », « 12 min », « 2 h 05 », « 1 j 3 h »."""
    seconds = int(max(0, round(float(seconds or 0))))
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    if seconds < 86400:
        minutes = (seconds % 3600) // 60
        return ("%d h %02d" % (seconds // 3600, minutes)) if minutes \
            else "%d h" % (seconds // 3600)
    return "%d d %d h" % (seconds // 86400, (seconds % 86400) // 3600)


def parse_within(within) -> float:
    """`--within` : secondes, ou une durée « 30m », « 2h » (format de
    `stagnation.parse_duration`)."""
    from . import stagnation

    if isinstance(within, (int, float)) and not isinstance(within, bool):
        if float(within) <= 0:
            raise WorkError("zero or negative deadline: %r" % within)
        return float(within)
    try:
        return stagnation.parse_duration(str(within or ""))
    except stagnation.StaleError as exc:
        raise WorkError("--within : %s" % exc) from exc


def delegate(db: Db, item_id: int, agent: str, *, within, actor: str = "",
             cfg=None) -> dict:
    """L40 (0030, point 5) : `ameesh work delegate <id> <agent> --within 30m`.

    Le délégant est `actor`, à défaut l'assigné actuel (un `human:…` est
    admis) ; c'est à lui que le lot reviendra. Le délégué passe par
    l'attribution gardée (`check_assignee`, mêmes refus que `work assign`) et
    doit être un AGENT : une échéance se mesure en tours. Une nouvelle
    délégation remplace la précédente (et son échéance). Un événement
    (`kind=event`, lié au lot) réveille le délégué.

    Rend `{item, delegation, replaced, check, previous, message_id, warning}`.
    """
    within_s = parse_within(within)
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if item["state"] in MERGED_STATES or item["state"] == "closed":
        raise WorkError("lot %s already %s: nothing to delegate" % (item_id, item["state"]))
    name = (agent or "").strip()
    if name.startswith(HUMAN_PREFIX):
        raise WorkError("delegation refused: %s is a human; a delegation with a "
                        "deadline goes to a wakeable agent (for a human: "
                        "ameesh work assign)" % name)
    previous = item.get("assignee")
    delegator = (actor or "").strip() or (previous or "").strip()
    if not delegator:
        raise WorkError("delegation refused: lot %s has no assignee; pass "
                        "--actor (the delegator, to whom the lot will return)" % item_id)
    if delegator == name:
        raise WorkError("delegation refused: %s would delegate the lot to itself" % name)
    if not delegator.startswith(HUMAN_PREFIX):
        from . import registry

        if registry.get(db, delegator) is None:
            raise WorkError("delegation refused: delegator %s is not in the "
                            "registry; the lot could not return to anyone" % delegator)
    check = check_assignee(db, name, cfg=cfg)
    if check["assignee"].startswith(HUMAN_PREFIX):
        # L46 : un nom nu normalisé en humain n'est pas un délégué possible
        raise WorkError("delegation refused: %s names a human (%s); a delegation "
                        "with a deadline goes to a wakeable agent (for a human: ameesh "
                        "work assign)" % (name, check["assignee"]))
    note = "delegated to %s by %s, due in %s (before: %s)" % (
        name, delegator, span(within_s), previous or "nobody")
    out = storage.of(db).work.delegate(
        item_id, name, current=previous, delegated_by=delegator, within_s=within_s,
        note=note, actor=delegator)
    if out is None:
        raise WorkError("lot %s changed in the meantime: retry" % item_id)
    out.update(check=check, previous=previous, message_id=None, warning=check.get("warning"))
    lot = out["item"]
    body = ("Lot #%d \"%s\": %s delegates it to you, to pick up within %s. Without a turn "
            "from you on this lot by the deadline, it returns to %s."
            % (int(lot["id"]), lot.get("title") or "", delegator, span(within_s), delegator))
    from . import mail

    try:
        out["message_id"] = mail.send(
            db, delegator, name, body, kind="event", work_item_id=str(lot["id"]),
            payload={"delegation": {"lot": int(lot["id"]), "delegated_by": delegator,
                                    "due_ts": lot.get("due_ts")}})
    except Exception as exc:  # noqa: BLE001 - la délégation est écrite ; le dire
        out["warning"] = "event not delivered to %s (%s): notify them" % (name, exc)
    return out


def _resolution_note(outcome: str, row: dict) -> str | None:
    """La ligne de journal d'une délégation échue (écrite avec son issue)."""
    if outcome == "rendue":
        return ("delegation expired: back to %s (no turn from %s on the lot since the "
                "delegation)" % (row["delegated_by"], row["delegate"]))
    if outcome == "soldee":
        return "delegation settled: %s worked on the lot before the deadline" % row["delegate"]
    return None


def expire_delegations(db: Db, now: float | None = None, *, actor: str = SYSTEM_SENDER,
                       dry_run: bool = False) -> list[dict]:
    """L40 (0030, point 5) : traite les délégations dont l'échéance est passée.

    Pour chaque délégation en cours échue (`due_at < now`) : sans AUCUN tour
    du délégué sur ce lot depuis son début (ni transition, note, jalon,
    action ou message lié de sa part), le lot revient au délégant (issue
    `rendue`, journal « délégation échue : retour à X », événement dans la
    boîte du délégant, qui le réveille) ; avec du travail constaté, la
    délégation est soldée (échéance effacée). Un lot fermé ou réassigné
    entre-temps : délégation annulée, sans bruit.

    Appelée à chaque passe de l'exécuteur ; deux exécuteurs simultanés ne
    rendent jamais deux fois (verrou de ligne et recontrôle, voir le
    stockage). `dry_run` : ce qui serait fait, rien n'est écrit. Rend une
    ligne par délégation traitée, avec `outcome` (et `message_id`).
    """

    now = time.time() if now is None else float(now)
    st = storage.of(db).work
    out: list[dict] = []
    for row in st.due_delegations(now):
        if dry_run:
            if row["state"] in MERGED_STATES or row["state"] == "closed" \
                    or row.get("assignee") != row["delegate"]:
                outcome = "annulee"
            else:
                outcome = "soldee" if row.get("worked") else "rendue"
            out.append(dict(row, outcome=outcome, dry_run=True, message_id=None))
            continue
        done = st.resolve_delegation(int(row["delegation_id"]), now_ts=now, actor=actor,
                                     describe=_resolution_note)
        if done is None:
            continue  # traitée entre-temps (autre exécuteur)
        done["message_id"] = None
        delegator = done["delegated_by"]
        if done["outcome"] == "rendue" and not delegator.startswith(HUMAN_PREFIX):
            from . import mail

            body = ("Lot #%d \"%s\": delegation expired, it is back to you. %s took no "
                    "turn on this lot before the deadline."
                    % (int(done["work_item_id"]), done.get("title") or "", done["delegate"]))
            try:
                done["message_id"] = mail.send(
                    db, SYSTEM_SENDER, delegator, body, kind="event",
                    work_item_id=str(done["work_item_id"]),
                    payload={"delegation_expired": {
                        "lot": int(done["work_item_id"]), "delegate": done["delegate"],
                        "due_ts": done.get("due_ts")}})
            except Exception as exc:  # noqa: BLE001 - le retour est fait ; l'alerte suit
                done["warning"] = "event not delivered to %s (%s)" % (delegator, exc)
        out.append(done)
    return out


def mark_delegate_turn(db: Db, agent: str, lots) -> list[int]:
    """L40 : un tour de `agent` commence sur ces lots (lot du tour, lots des
    messages du tour). Ceux qui lui sont délégués reçoivent leur premier tour,
    une seule fois : la délégation ne reviendra pas au délégant."""
    ids = sorted({int(str(lot).strip()) for lot in lots or ()
                  if str(lot or "").strip().isdigit()})
    if not ids:
        return []
    return storage.of(db).work.mark_delegate_turn(
        agent, ids, "delegation: turn of %s on the lot" % agent)


def delegation_view(item: dict, now: float | None = None) -> dict | None:
    """La délégation d'un lot, pour `work list`/`work show`/`progress` (L40).

    None hors délégation (ou lot fermé). Sinon `{delegated_by, delegate,
    delegated_ts, due_ts, due_in_s, overdue, settled, label}` ; `due_in_s`
    est négatif en retard ; `settled` : délégation soldée (le délégué a
    travaillé, l'échéance est effacée).
    """

    if not item.get("delegated_by") or item.get("state") in MERGED_STATES \
            or item.get("state") == "closed":
        return None
    now = time.time() if now is None else float(now)
    due = item.get("due_ts")
    due_in = int(round(float(due) - now)) if due is not None else None
    overdue = due_in is not None and due_in < 0
    who = "delegated by %s" % item["delegated_by"]
    if due is None:
        label = "%s, deadline settled (work seen)" % who
    elif overdue:
        label = "%s, overdue by %s" % (who, span(-due_in))
    else:
        label = "%s, due in %s" % (who, span(due_in))
    return {"delegated_by": item["delegated_by"], "delegate": item.get("assignee"),
            "delegated_ts": item.get("delegated_ts"), "due_ts": due,
            "due_in_s": due_in, "overdue": overdue, "settled": due is None,
            "label": label}


#: sortes de fiche auxquelles un lot se rattache (un jalon regroupe des epics)
LINKABLE_KINDS = ("lot", "epic")


def _package(db: Db, ident: str) -> dict:
    """La fiche WorkPackage `ident`, présente au canon synchronisé, sinon WorkError."""
    fiche = storage.of(db).packages.get(ident)
    if fiche is None:
        raise WorkError("unknown WorkPackage card: %r (declare it in the canon, then "
                        "run \"ameesh canon sync\")" % ident)
    if not fiche.get("present"):
        raise WorkError("WorkPackage card %s removed from the canon" % ident)
    if fiche.get("kind") not in LINKABLE_KINDS:
        raise WorkError("card %s: a lot links to a %s, not to a %s"
                        % (ident, " or ".join(LINKABLE_KINDS), fiche.get("kind")))
    return fiche


def link(db: Db, item_id: int, package: str | None, *, actor: str = "") -> dict:
    """Rattache un lot existant à une fiche WorkPackage (None : le détache)."""
    if get(db, item_id) is None:
        raise WorkError("lot %s not found" % item_id)
    fiche = _package(db, package) if package else None
    row = storage.of(db).work.link_package(
        item_id, fiche["id"] if fiche else None, fiche.get("parent") if fiche else None,
        note="plan: %s" % package if package else "plan: unlinked", actor=actor)
    if row is None:
        raise WorkError("lot %s not found" % item_id)
    return row


def close_merged(db: Db, item_id: int, *, sha: str = "", actor: str = "", source: str = "",
                 pr_ref: str | None = None, frozen_id: int | None = None,
                 declared: bool = False, extra: str = "") -> dict:
    """Fusion constatée : le lot passe `merged` et son jalon `merged` est posé.

    Idempotent : un lot déjà fusionné reste tel quel (`already`). Jamais de
    réouverture silencieuse : un lot fermé (abandonné, remplacé) n'est pas
    touché, le constat est rendu (`refused`) pour être dit à l'appelant. Rend
    `{id, result: merged|already|refused|refrozen, detail, item}`.

    `frozen_id` (preuve par le contenu) : le jalon `frozen` examiné ; si ce
    n'est plus le dernier gel du lot au moment d'écrire, rien n'est fermé
    (`refrozen`) — le nouveau gel n'a pas été examiné.

    La note du journal et du jalon dit comment la fusion est connue :
    « fusion constatée par <source> », ou « fusion déclarée par … »
    (`declared`, `ameesh work merged`), suivie de `extra`.
    """
    for _attempt in range(3):
        item = get(db, item_id)
        if item is None:
            raise WorkError("lot %s not found" % item_id)
        state = item["state"]
        if state in MERGED_STATES:
            return {"id": int(item_id), "result": "already", "item": item,
                    "detail": "already %s" % state}
        if state == "closed":
            return {"id": int(item_id), "result": "refused", "item": item,
                    "detail": "lot closed (%s): merge seen%s, not reopened"
                              % (item.get("close_reason"), " (%s)" % pr_ref if pr_ref else "")}
        note = "merge %s%s%s%s%s" % (
            "declared" if declared else "seen",
            " by %s" % source if source else "", " — %s" % pr_ref if pr_ref else "",
            " (commit %s)" % sha[:12] if sha else "", " — %s" % extra if extra else "")
        try:
            row = storage.of(db).work.close_merged(
                item_id, current=state, sha=sha, actor=actor, note=note, pr_ref=pr_ref,
                frozen_id=frozen_id)
        except DbError as exc:
            # A refused module requirement is a per-lot result, allowing a
            # merge sweep to continue with other projects. Other DB errors
            # remain errors; the storage transaction already rolled back.
            if "AMEESH_MODULE_RULE:" not in str(exc):
                raise
            return {"id": int(item_id), "result": "refused", "item": item,
                    "detail": str(exc)}
        if row is not None:
            return {"id": int(item_id), "result": "merged", "item": row,
                    "detail": "%s → merged" % state}
        if frozen_id is not None:
            freezes = [m for m in milestones(db, item_id) if m["kind"] == "frozen"]
            if not freezes or int(freezes[0]["id"]) != int(frozen_id):
                return {"id": int(item_id), "result": "refrozen", "item": get(db, item_id),
                        "detail": "new freeze declared in the meantime: not closed, review again"}
    raise WorkError("lot %s moved in the meantime: retry" % item_id)


def merged(db: Db, item_id: int, *, sha: str, note: str = "", actor: str = "") -> dict:
    """`ameesh work merged <id> --sha S` : une fusion faite hors PR et sans gel
    (fusion locale sur la branche cible) se déclare en une fois.

    Le lot passe `merged` depuis tout état ouvert (intake, build, qa,
    blocked, waiting_human) ; son jalon `merged` reçoit le commit de fusion et
    l'acteur ; le journal dit « fusion déclarée par <acteur> (commit …) ».
    Idempotent (`already`, rien n'est réécrit) ; un lot fermé (abandonné,
    remplacé) n'est jamais rouvert : WorkError. Rend le constat de
    `close_merged`."""
    text = (sha or "").strip().lower()
    if not _SHA_RE.match(text):
        raise WorkError("--sha: the merge commit (7 to 64 hex characters), not %r"
                        % (sha,))
    done = close_merged(db, item_id, sha=text, actor=actor, source=actor or UNKNOWN_ACTOR,
                        declared=True, extra=(note or "").strip())
    if done["result"] == "refused":
        item = done["item"]
        if item["state"] != "closed":
            raise WorkError(done["detail"])
        raise WorkError("lot %s closed (%s): not reopened; see ameesh work show %s"
                        % (item_id, "abandoned" if item.get("close_reason") == "abandoned"
                           else "superseded by #%s" % item.get("superseded_by"), item_id))
    return done


def close(db: Db, item_id: int, *, abandoned: bool = False, superseded_by: int | None = None,
          note: str = "", actor: str = "") -> dict:
    """Ferme un lot qui ne sera pas fusionné : abandonné, ou remplacé par un
    autre lot. Jalon `closed` ; rien ne reste ouvert par oubli."""
    if bool(abandoned) == (superseded_by is not None):
        raise WorkError("pass --abandoned OR --superseded-by <id>")
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if superseded_by is not None:
        if int(superseded_by) == int(item_id):
            raise WorkError("a lot cannot supersede itself")
        if get(db, superseded_by) is None:
            raise WorkError("superseding lot %s not found" % superseded_by)
    state = item["state"]
    if state in MERGED_STATES or state == "closed":
        raise WorkError("lot %s already %s: nothing to close" % (item_id, state))
    reason = "abandoned" if abandoned else "superseded"
    text = ("abandoned" if abandoned else "superseded by #%s" % superseded_by) + (
        " — %s" % note.strip() if note and note.strip() else "")
    row = storage.of(db).work.close(
        item_id, current=state, reason=reason,
        superseded_by=int(superseded_by) if superseded_by is not None else None,
        actor=actor, note=text)
    if row is None:
        raise WorkError("lot %s moved in the meantime: retry" % item_id)
    return row


def get(db: Db, item_id: int) -> dict | None:
    return storage.of(db).work.get(item_id)


def list_items(db: Db, *, state: str | None = None, assignee: str | None = None,
               limit: int = 50) -> list[dict]:
    if state and state not in STATES:
        raise WorkError("unknown state: %r (%s)" % (state, ", ".join(STATES)))
    return storage.of(db).work.items(state=state, assignee=assignee, limit=limit)


def move(db: Db, item_id: int, state: str, *, note: str = "", actor: str = "",
         correct: str | None = None) -> dict:
    """Déplace un lot en vérifiant la transition, la boucle QA et le terminal.

    `correct` (la raison) : correction d'un état posé par erreur, hors machine
    à états, limitée à `CORRECTIONS` (un `promoted` à tort revient en
    `merged`) ; le journal la dit (« correction : promoted → merged — raison »).
    L'appelant a vérifié qui corrige (`corrector`)."""
    if state not in STATES:
        raise WorkError("unknown state: %r (%s)" % (state, ", ".join(STATES)))
    if state == "closed":
        raise WorkError("to close a lot: ameesh work close <id> --abandoned | "
                        "--superseded-by <id>")
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    current = item["state"]
    if current == state:
        raise WorkError("lot %s already in %s" % (item_id, state))
    if correct is not None:
        reason = (correct or "").strip()
        if not reason:
            raise WorkError("--correct: the reason for the correction is required")
        if (current, state) not in CORRECTIONS:
            raise WorkError(
                "correction refused: %s → %s (possible corrections: %s; otherwise the "
                "transitions allowed from %s: %s)"
                % (current, state, ", ".join("%s → %s" % c for c in sorted(CORRECTIONS)),
                   current, ", ".join(sorted(TRANSITIONS[current])) or "none"))
        text = "correction: %s → %s — %s%s" % (
            current, state, reason, " (%s)" % note.strip() if note and note.strip() else "")
        row = storage.of(db).work.move(item_id, state, current=current, loops=0, note=text,
                                       actor=actor)
        if row is None:
            raise WorkError("lot %s moved in the meantime: retry" % item_id)
        return row
    if state not in TRANSITIONS[current]:
        raise WorkError(
            "transition refused: %s → %s (allowed from %s: %s)%s"
            % (current, state, current, ", ".join(sorted(TRANSITIONS[current])) or "none",
               " — state set by mistake: ameesh work move %s %s --correct \"reason\""
               % (item_id, state) if (current, state) in CORRECTIONS else ""))
    if state == "waiting_human" and not storage.of(db).decisions.pending_for_lot(str(item_id)):
        raise WorkError(WAITING_WITHOUT_QUESTION % {"id": item_id})
    returning_loop = current == "qa" and state == "build"
    if returning_loop and int(item["loops"]) >= MAX_QA_LOOPS:
        raise WorkError(
            "qa → build loop exhausted (%d): move the lot to blocked, or ask the "
            "owner: ameesh decide ask --lot %s --question \"…\""
            % (MAX_QA_LOOPS, item_id))

    held = state == "waiting_human"
    row = storage.of(db).work.move(
        item_id, state, current=current, loops=1 if returning_loop else 0,
        note=note or ("%s → %s" % (current, state)), actor=actor, held=held)
    if row is None:
        if held and not storage.of(db).decisions.pending_for_lot(str(item_id)):
            # the request was answered or withdrawn meanwhile
            raise WorkError(WAITING_WITHOUT_QUESTION % {"id": item_id})
        raise WorkError("lot %s moved in the meantime: retry" % item_id)
    return row


def note(db: Db, item_id: int, text: str, *, actor: str = "") -> bool:
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if not (text or "").strip():
        raise WorkError("empty note")
    storage.of(db).work.note(item_id, item["state"], text.strip(), actor)
    return True


def events(db: Db, item_id: int, limit: int = 100) -> list[dict]:
    return storage.of(db).work.events(item_id, limit)


#: les jalons d'un lot : `requested` et `merged` sont automatiques (0013),
#: `frozen` et `verdict` se déclarent par la CLI.
MILESTONE_KINDS = ("requested", "frozen", "verdict", "merged", "closed")
MANUAL_KINDS = ("frozen", "verdict")
VERDICTS = ("ok", "blocked")

#: les jalons affichés, du plus récent au plus ancien : (nom, colonne d'instant,
#: colonne de durée) — l'ordre est celui de la frise lue à l'envers.
MILESTONES = (
    ("merge", "merged_ts", "review_to_merge_s"),
    ("review", "reviewed_ts", "freeze_to_review_s"),
    ("freeze", "frozen_ts", "request_to_freeze_s"),
    ("request", "requested_ts", None),
)

#: les durées affichées et leur colonne (la forme de `delays` est stable, L24)
MILESTONE_DURATIONS = (
    ("request → freeze", "request_to_freeze_s"),
    ("freeze → review", "freeze_to_review_s"),
    ("freeze → merge", "freeze_to_merge_s"),
    ("total", "total_s"),
)


def milestones(db: Db, item_id: int, limit: int = 100) -> list[dict]:
    """Les jalons d'un lot, du plus récent au plus ancien."""
    return storage.of(db).work.milestones(item_id, limit)


def milestone(
    db: Db,
    item_id: int,
    kind: str,
    *,
    sha: str = "",
    actor: str = "",
    verdict: str | None = None,
    note: str = "",
    on_frozen=None,
    after_commit: list | None = None,
) -> dict:
    """Déclare un jalon que la machine à états ne peut pas déduire.

    `frozen` (la branche est gelée pour la revue) et `verdict` (`ok` ou
    `blocked`, avec le `sha` relu) sont les deux seuls : `requested` et `merged`
    sont écrits automatiquement depuis les transitions (0013), les déclarer à la
    main serait un mensonge sur la frise.

    `on_frozen` (facultatif, #1617) : le callback du gel, appelé dans la
    transaction du jalon `frozen` avec `(tx, milestone, after_commit)`. Il
    laisse `after_commit` recevoir les écritures de fil différées
    (`mail.send(..., after_commit=…)`) ; `milestone` vide la liste après le
    commit. Absent (le défaut), le gel ne désigne personne : le raccord de la
    désignation des relecteurs se branche à l'activation du lot #1617, pas ici.
    """
    if kind not in MILESTONE_KINDS:
        raise WorkError("unknown milestone: %r (%s)" % (kind, ", ".join(MILESTONE_KINDS)))
    if kind not in MANUAL_KINDS:
        raise WorkError(
            "milestone %s is automatic (lot transition or closing): only %s "
            "are declared by hand" % (kind, ", ".join(MANUAL_KINDS)))
    if kind == "verdict":
        if verdict not in VERDICTS:
            raise WorkError("missing or unknown verdict: %r (%s)"
                            % (verdict, ", ".join(VERDICTS)))
    elif verdict is not None:
        raise WorkError("a verdict only makes sense for the `verdict` milestone")
    if on_frozen is not None and kind != "frozen":
        raise WorkError("a freeze callback only makes sense for the `frozen` milestone")
    if get(db, item_id) is None:
        raise WorkError("lot %s not found" % item_id)
    return storage.of(db).work.add_milestone(
        item_id, kind, sha=sha.strip(), actor=actor, verdict=verdict, note=note.strip(),
        on_frozen=on_frozen, after_commit=after_commit)


def delays(db: Db, limit: int = 200, *, ids: Sequence[int] | None = None) -> list[dict]:
    """Les délais par lot (R19), du plus récemment modifié au plus ancien.

    `ids` restreint le balayage à ces lots : `work list` le passe pour que les
    délais d'une ligne filtrée ne dépendent pas de la fenêtre globale des lots
    les plus récents (un lot ancien garde sa frise — revue codex2, B3).
    """
    return storage.of(db).work.delays(limit, ids=ids)


def timeline(db: Db, item_id: int) -> dict | None:
    """Les jalons et durées d'un lot, ou None."""
    return storage.of(db).work.timeline(item_id)


def last_milestone(row: dict) -> tuple[str, float | None]:
    """Le dernier jalon atteint par un lot, et son instant (epoch).

    « demande » quand le lot n'est pas encore gelé, puis « gel », « revue »
    (verdict rendu) et « fusion ». Sert à dater ce qu'on attend : l'âge du
    dernier jalon est le temps que le lot passe dans sa phase courante.
    """
    for name, at_key, _duration in MILESTONES:
        if row.get(at_key):
            return name, float(row[at_key])
    return "request", float(row["requested_ts"]) if row.get("requested_ts") else None


def durations(row: dict) -> list[tuple[str, float | None]]:
    """Les durées de la frise, dans l'ordre : demande → gel → revue → fusion.

    Les compteurs (`verdicts`, `blocked_verdicts`) restent dans la ligne : ce
    ne sont pas des durées, et les confondre afficherait « 1s » pour un verdict.
    """
    return [(label, row.get(key)) for label, key in MILESTONE_DURATIONS]
