# SPDX-License-Identifier: AGPL-3.0-only
"""work_items — la machine à états des lots, reprise d'un orchestrateur de
tickets existant (spec §5 point 4).

    intake → build → qa → merged → promoted

plus `blocked` et `waiting_human`, et l'état terminal `closed` (abandonné,
remplacé par un autre lot, ou livré hors cible : `close`, L29 et #988). La
boucle `qa → build` est bornée à deux
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

from ameesh import work_lifecycle as lifecycle

import os
import re
import time
from typing import Sequence
from urllib.parse import quote

from . import storage
from .db import Db, DbError, is_unique_violation
from .storage import interface
from .storage.postgres import work as _pg

# Public compatibility exports; the vocabulary is also usable without importing
# storage or the command layer (housekeeping, progress and capacity readers).
from .work_lifecycle import (ACTIVE, COMPLETED, DELIVERED, STATES, TERMINAL,
                             TO_VALIDATE, WAITING)
#: déjà fusionnés : une fusion constatée n'y change rien
MERGED_STATES = DELIVERED
#: raisons d'une fermeture explicite (`ameesh work close`) : abandonné,
#: remplacé par un autre lot, livré hors cible avec sa tête relue (#988),
#: ou livré sans fusion (`done`, #1280)
CLOSE_REASONS = ("abandoned", "superseded", "delivered", "done")
#: la source d'un lot de relecture après fusion (review_lot.PREFIX) : un lot
#: sans branche, dont l'identité est le parent relu. work.py ne peut pas
#: importer review_lot (cycle), la constante est la même chaîne.
REVIEW_SOURCE_PREFIX = "review-after-merge:"
#: le titre d'un lot de relecture après fusion (review_lot.title_of), repris
#: par les relectures ouvertes à la main : `REVUE #<parent> : …`.
REVIEW_TITLE_RE = re.compile(r"^\s*REVUE\s*#\s*(\d+)\b", re.IGNORECASE)
#: expéditeur des événements qu'ameesh écrit lui-même (échéance d'une
#: délégation, rafraîchissement d'un lot généré par la veille des versions)
SYSTEM_SENDER = "ameesh"
#: #1280 : les missions de relecture ouvertes à la main hors convention
#: `REVUE #N` : `R1 <harnais> #<parent> …` (cas #1249), et ses variantes
#: observées `R1 delta Codex #1037 r2`, `R1 Codex de #1066`. La forme est
#: ancrée en tête (`R1`/`R2`) et le parent est le premier `#<id>` de
#: l'en-tête, avant le `:` ou le tiret de la description : un titre qui
#: commence par `R1`/`R2` sans `#<id>` reste une mission (parent inconnu,
#: donc refusée), jamais un lot ordinaire.
MANUAL_REVIEW_START_RE = re.compile(r"^\s*R[12]\b", re.IGNORECASE)
MANUAL_REVIEW_TITLE_RE = re.compile(r"^\s*R[12]\b[^:—–#]*#\s*(\d+)\b", re.IGNORECASE)
MAX_QA_LOOPS = 2
# The development module owns its stages and compatibility transitions.
from .modules import registry as _module_registry


def __getattr__(name):
    # Compatibility exports are resolved only when development tooling uses
    # them. Importing the generic core must also work in the bare profile.
    if name in {"FORWARD", "TRANSITIONS", "CORRECTIONS"}:
        return getattr(_module_registry.development_workflow(), name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))

#: rôles d'une fiche Agent du canon admis à corriger un état (avec les humains)
CORRECTOR_ROLES = ("orchestrateur", "orchestrator", "conception", "design")
#: §4 : marqueur d'une exception pilote EXPLICITE à la limite d'en-cours,
#: portée par la note de la dernière attribution du lot (jamais une exemption
#: silencieuse de qui a assigné le lot).
WIP_EXCEPTION_MARK = "WIP exception"
#: préfixes d'une note d'attribution — le journal les relit pour savoir si la
#: dernière attribution d'un lot porte le marqueur d'exception (`wip_exceptions`)
#: et si un retour §4 lui est postérieur (`storage.interface.ASSIGN_NOTE_PREFIXES`)
ASSIGN_NOTES = interface.ASSIGN_NOTE_PREFIXES
#: §4 : début de la note de journal d'un lot RENDU à la file — un lot `build`
#: sans assigné n'est prenable par la prise automatique que s'il a été rendu
#: ainsi (`storage.interface.RETURNED_NOTE`)
RETURN_NOTE = interface.RETURNED_NOTE
#: §4 : états d'un lot que le geste `work return` rend à la file. Un lot en
#: revue (`qa`), en attente d'un humain ou bloqué n'est pas de l'en-cours d'un
#: agent : il ne se rend pas par ce geste.
RETURN_STATES = ACTIVE
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
#: #2954 : les types de lot que porte la base (mêmes valeurs que la projection
#: GitHub, `plan_github.LOT_TYPES`). Un lot `improvement` est un panier, avec
#: ses champs de valeur ; `bug` et `evolution` suivent le chemin de correction.
LOT_TYPES = ("bug", "evolution", "improvement")
#: #2954 : les cibles de `work set-type` — `bug` ↔ `evolution`. Un
#: `improvement` est créé par la file d'amélioration (il exige `expected_value`
#: et `value_score`, contrainte 0049) ; on peut en sortir, pas y entrer par ce
#: geste.
RETYPE_TARGETS = ("bug", "evolution")
#: L233: a lot enters `waiting_human` only while an owner decision request
#: (`ameesh decide ask`) is open for it; a decision written in prose in mail
#: never counts (the owner was shown lots "waiting for them" with no question)
WAITING_WITHOUT_QUESTION = (
    "refused: lot %(id)s enters waiting_human only through an open owner decision "
    "request — ameesh decide ask --lot %(id)s --question \"…\" --option a=\"…\" (the "
    "lot moves to waiting_human by itself); a decision written in a mail never counts")

#: colonnes rendues pour un lot (pilote Postgres ; alias de compatibilité).
#: Depuis #705 c'est une fonction : la colonne `reviewer` n'existe qu'après la
#: migration 0421, et la prod peut être en 0373.
def item_columns(db) -> str:
    return _pg.item_columns(db)


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
        from .agent_names import resolve
        return resolve(text, db=db, operation="work.actor"), None
    binding = identity.resolve_binding(cfg, db)
    if binding.name:
        if binding.ok:
            return binding.name, None
        return binding.name, ("identity %s not bound (%s): used as actor"
                              % (binding.name, binding.reason or "unknown reason"))
    return UNKNOWN_ACTOR, ("unknown actor: no identity bound to this session "
                           "(AGENT_MAIL_NAME, ameesh mail bind); the lot journal will say "
                           "\"%s\"; pass --actor <agent|human:id>" % UNKNOWN_ACTOR)


def is_auditor(cfg, db, name: str) -> bool:
    """#3324 : `name` est-il l'auditeur — la persona `auditeur`, ou un agent
    dont la fiche Agent du canon porte `roles: [auditeur]` (ou `auditor`) ?

    La fiche de l'auditeur (canon, 24/10/2026) range la redésignation d'un
    relecteur au niveau 1 ; le plancher d'avis (`--floor`) reste un geste de
    pilote. Un auditeur arrêté ou mort reste un acteur pour ce geste : c'est
    son identité qui signe la désignation, pas un tour de travail."""
    from . import alert_briefs, canon, registry

    who = (name or "").strip()
    if not who:
        return False
    try:
        listing = registry.overview(db)
        canons = canon.load_configured(cfg) if cfg is not None else []
        return who in alert_briefs.auditors(listing, canons)
    except Exception:  # noqa: BLE001 - autorité illisible : refus, comme avant
        return False


def corrector(cfg, db, explicit: str | None = None, *, what: str = "correction",
              history: bool = True, auditor: bool = False) -> str:
    """Qui corrige un état posé par erreur (`work move … --correct`) ; WorkError
    si la correction lui est refusée. `what` (#705) nomme le geste dans les
    messages : la désignation du relecteur demande les mêmes autorités.

    Réservée aux humains et aux orchestrateurs ou agents de conception. Une
    session qui porte une identité d'agent (`identity.resolve_binding`) corrige
    en son nom, et seulement si cet agent est orchestrateur ou de conception
    (`assignments.is_orchestrator` avec les rôles `CORRECTOR_ROLES` : rôle de sa
    fiche Agent au canon, `AMEESH_ALERT_ORCHESTRATORS`, ou lots confiés
    récemment) ; elle ne peut pas corriger au nom d'un autre. Une session sans
    identité d'agent (un humain à son terminal) dit qui corrige : `--actor
    human:<id>`, un humain connu, ou un orchestrateur. `history=False` (#740) :
    les lots confiés récemment ne comptent pas, seul un rôle déclaré compte.

    `auditor=True` (#3324) ajoute l'auditeur aux autorités : la désignation et
    le retrait d'un relecteur lui sont ouverts (fiche du 24/10, niveau 1), pas
    le plancher d'avis (`review_floor.set_floor` garde le défaut)."""
    from . import assignments, identity, registry

    text = (explicit or "").strip()
    from .agent_names import resolve
    text = resolve(text, db=db, operation="work.corrector")
    binding = identity.resolve_binding(cfg, db)
    # A rejected runner/session identity must never fall back to the human
    # terminal path, including a malformed AGENT_MAIL_NAME (no returned name).
    if not binding.ok and (binding.name or binding.source != "none"
                           or os.environ.get("AGENT_MAIL_NAME")):
        raise WorkError("%s refused: invalid session identity (%s)"
                        % (what, binding.reason or "unbound identity"))
    refus = ("%s refused: %%s is neither a human, an orchestrator nor a design agent "
             "(orchestrator or design role on its Agent card in the canon, "
             "AMEESH_ALERT_ORCHESTRATORS); ask the orchestrator or the owner" % what)
    if auditor:
        refus = ("%s refused: %%s is neither a human, an orchestrator, a design "
                 "agent nor the auditor (orchestrator, design or auditor role on "
                 "its Agent card in the canon, AMEESH_ALERT_ORCHESTRATORS); ask "
                 "the orchestrator or the owner" % what)
    if binding.name:
        if text and text != binding.name:
            raise WorkError("%s refused: this session belongs to %s, it does not "
                            "act on behalf of %s" % (what, binding.name, text))
        if assignments.is_orchestrator(cfg, db, binding.name, roles=CORRECTOR_ROLES,
                                       history=history):
            return binding.name
        if auditor and is_auditor(cfg, db, binding.name):
            return binding.name
        raise WorkError(refus % binding.name)
    if not text:
        # le libellé historique de la correction reste (la CLI `hosts` le
        # traduit en « hold: say who holds », #254) ; `what` nomme les autres
        # gestes (#705, « designation »)
        if what == "correction":
            raise WorkError("correction: say who corrects (--actor human:<id>)")
        raise WorkError("%s: say who does it (--actor human:<id>)" % what)
    if text.startswith(HUMAN_PREFIX):
        return text
    human = known_human(db, text, cfg)
    if human:
        return human
    if registry.get(db, text) is not None \
            and assignments.is_orchestrator(cfg, db, text, roles=CORRECTOR_ROLES,
                                            history=history):
        return text
    if auditor and registry.get(db, text) is not None \
            and is_auditor(cfg, db, text):
        return text
    raise WorkError(refus % text)


def claims_pilot(cfg, db, who: str) -> bool:
    """#1664 : `who` se présente comme pilote — un humain (`human:<id>`) ou un
    agent orchestrateur/conception, la même autorité que `corrector`
    (`history=False` : seul un rôle déclaré compte, pas l'historique des lots
    confiés)."""
    from . import assignments

    who = (who or "").strip()
    if not who:
        return False
    if who.startswith(HUMAN_PREFIX):
        return True
    return assignments.is_orchestrator(cfg, db, who, roles=CORRECTOR_ROLES,
                                       history=False)


def _active_holds(db, item_id: int | None) -> dict[str, dict]:
    """#3219 : les retenues actives d'un lot, par auteur ({} sans retenue).

    Chaque hold actif retient de lui-même (`planned_waits.active_holds`) : le
    hold d'un pilote ou d'un tiers reste actif sous celui de l'assigné, et la
    levée de l'assigné ne retire que le sien. La lecture est la lecture
    groupée ciblée (`lot_hold_events`), la même que
    `plan_git.content_retention` : les notes ordinaires ne sont pas lues. Sur
    une connexion d'exécuteur médié (#109), `progress` n'est pas au contrat :
    le journal reste au serveur et une levée ordinaire y est refusée comme
    avant #3219."""
    if item_id is None or storage.is_remote(db):
        return {}
    from . import planned_waits

    lot = int(item_id)
    events = storage.of(db).progress.lot_hold_events(
        [lot], planned_waits.HOLD_NOTE_PATTERNS)
    return planned_waits.active_holds(db, events).get(lot) or {}


def _own_hold_lift(cfg, db, actor: str, holds: dict[str, dict]) -> str:
    """#3219 : l'auteur d'une retenue active lève la sienne — et lui seul.

    La levée ordinaire est permise à l'acteur qui a posé une retenue encore
    active, à condition qu'il la prouve : la session liée n'écrit qu'en son
    nom (`--actor` usurpé refusé, comme `forcer`/`milestone_actor`), une
    session sans liaison ne nomme qu'un humain (qui est pilote, et passe donc
    par `corrector`), et un tiers ordinaire est refusé. Les autres retenues
    du lot — pilote, humain, QA — ne sont pas effacées par cette levée
    (`planned_waits.active_holds`) : seule une levée de pilote les lève
    toutes (#1664 r2 B1)."""
    from . import identity

    what = "hold lift"
    binding = identity.resolve_binding(cfg, db)
    if not binding.ok and (binding.name or binding.source != "none"
                           or os.environ.get("AGENT_MAIL_NAME")):
        raise WorkError("%s refused: invalid session identity (%s)"
                        % (what, binding.reason or "unbound identity"))
    if binding.name:
        if actor != binding.name:
            raise WorkError(
                "%s refused: this session belongs to %s, it does not act on behalf "
                "of %s" % (what, binding.name, actor or UNKNOWN_ACTOR))
        if actor not in holds:
            raise WorkError(
                "%s refused: %s holds no active hold on this lot (held by %s); only "
                "the author of a hold or a pilot (human, orchestrator or design) can "
                "lift it" % (what, actor, _hold_holders(holds)))
        return actor
    # sans liaison, seuls les gestes d'un terminal humain sont attribuables ;
    # un humain est pilote et n'arrive donc pas ici en pratique, mais la règle
    # reste explicite si l'autorisation change.
    if not (actor.startswith(HUMAN_PREFIX) and len(actor) > len(HUMAN_PREFIX)):
        raise WorkError(
            "%s refused: %s is not a human and this session has no bound agent "
            "identity; only the author of a hold or a pilot can lift it"
            % (what, actor or UNKNOWN_ACTOR))
    if actor not in holds:
        raise WorkError(
            "%s refused: %s holds no active hold on this lot (held by %s); only "
            "the author of a hold or a pilot (human, orchestrator or design) can "
            "lift it" % (what, actor, _hold_holders(holds)))
    return actor


def _hold_holders(holds: dict[str, dict]) -> str:
    """Les auteurs des retenues actives, pour un message actionnable."""
    names = sorted({str(hold.get("by") or "?") for hold in (holds or {}).values()})
    return ", ".join(names) or "?"


def check_wait_note(cfg, db, actor: str, text: str, *,
                    item_id: int | None = None) -> str:
    """#1664 r2 (B1) : un hold de pilote s'authentifie au chemin d'écriture.

    `hold: <raison>` sert deux conventions : la retenue du réveil (règle 1),
    que tout assigné pose, et le hold de pilote (#1664), qui éteint la
    stagnation. Le journal ne garde que le texte et l'acteur : sans cette
    garde, une session ordinaire écrivait `--actor human:<pilote>` (ou le nom
    d'un pilote) et la relecture la croyait pilote. Un acteur qui se dit
    pilote doit donc le prouver ici (`corrector`, `history=False`). La note
    `hold:` d'un acteur ordinaire reste permise : c'est la règle 1, et elle ne
    compte pas pour la stagnation (`planned_waits.pilot_holds`).

    #3219 : une levée (`unhold:`…) n'est plus toujours un geste de pilote.
    L'auteur d'une retenue active peut lever la sienne (la passe de contenu
    #2744 le lui demande) ; les retenues des autres — pilote, humain, QA —
    restent actives et ne sont levées que par un pilote. Sans lot lu
    (`item_id` absent), la levée reste réservée au pilote."""
    text = (text or "").strip()
    if not text:
        return actor
    from . import planned_waits

    if planned_waits.is_lift_note(text):
        holds = _active_holds(db, item_id)
        # retenue absente (connexion médiée, lot sans hold), acteur qui se dit
        # pilote, ou acteur sans retenue active à lui : la garde #1664 r2 (B1)
        # s'applique telle quelle.
        if not holds or claims_pilot(cfg, db, actor) or actor not in holds:
            return corrector(cfg, db, actor, what="hold lift", history=False)
        return _own_hold_lift(cfg, db, actor, holds)
    if planned_waits.is_hold_lookalike(text):
        raise WorkError(
            "note looks like a hold but is not one: write exactly "
            "\"hold: <reason>\" to hold (or \"unhold: <reason>\" to lift); "
            "variants such as \"hold :\", \"hold (pilote) :\" or \"Hold —\" "
            "stay ordinary notes and stop nothing (#3373)")
    if planned_waits.is_hold_note(text) and claims_pilot(cfg, db, actor):
        return corrector(cfg, db, actor, what="hold", history=False)
    return actor


def forcer(cfg, db, explicit: str | None = None) -> str:
    """#540 : l'auteur d'un `--force "<raison>"`, écrit au journal du lot ;
    WorkError si la session ne peut pas le prouver. Une session qui porte une
    identité d'agent force en son nom, jamais au nom d'un autre (`--actor` ou
    `--from` différent : refus) ; une identité refusée ne retombe pas sur le
    terminal humain. Une session sans identité (un humain à son terminal)
    dit qui force, et seulement par une identité humaine explicite
    (`--actor human:<id>`) : sans liaison, un nom d'agent est refusé (revue
    r2, B/P2) — sinon n'importe quelle session signerait un forçage au nom
    d'un agent."""
    from . import identity

    text = (explicit or "").strip()
    binding = identity.resolve_binding(cfg, db)
    if not binding.ok and (binding.name or binding.source != "none"
                           or os.environ.get("AGENT_MAIL_NAME")):
        raise WorkError("--force refused: invalid session identity (%s)"
                        % (binding.reason or "unbound identity"))
    if binding.name:
        if text and text != binding.name:
            raise WorkError("--force refused: this session belongs to %s, it does not "
                            "force on behalf of %s" % (binding.name, text))
        return binding.name
    if not text:
        raise WorkError("--force: say who forces (--actor human:<id>)")
    if text.startswith(HUMAN_PREFIX) and len(text) > len(HUMAN_PREFIX):
        return text
    raise WorkError(
        "--force refused: %s is not a human and this session has no bound agent "
        "identity (runner lease or `ameesh mail bind`); an agent forces from its "
        "own bound session, a human terminal says --actor human:<id>" % text)


def publisher(cfg, db, explicit: str | None = None) -> str:
    """#3501 : l'auteur de `work estimates --publish` (L157b), le passage de
    l'auditeur ; WorkError pour tout autre. Chaque publication renvoyait les
    briefs `estimate_bias` à tous les estimateurs biaisés : le 25/10, un agent
    a republié la table une minute après l'auditeur pour clore sa propre
    alerte. Une session liée (bail du runner ou liaison de session,
    `identity.BOUND_SOURCES`, L244) publie en son nom, et seulement si cet
    agent est l'auditeur (`is_auditor`) ; un `AGENT_MAIL_NAME` seulement
    auto-déclaré ne suffit pas, et une identité refusée ne retombe pas sur le
    terminal humain. Une session sans identité ne nomme qu'un humain
    (`--actor human:<id>`) : sans liaison, un nom d'agent est refusé (revue
    de mty-amh-impl-05, comme #540 r2 pour `forcer`)."""
    from . import identity
    from .agent_names import resolve

    text = resolve((explicit or "").strip(), db=db, operation="work.publisher")
    binding = identity.resolve_binding(cfg, db)
    if not binding.ok and (binding.name or binding.source != "none"
                           or os.environ.get("AGENT_MAIL_NAME")):
        raise WorkError("--publish refused: invalid session identity (%s)"
                        % (binding.reason or "unbound identity"))
    refus = ("--publish refused: %s is not the auditor; the reference table (L157b) "
             "is the auditor's pass (persona auditeur, or auditeur role on its Agent "
             "card in the canon); work estimates without --publish reads it")
    if binding.name:
        if text and text != binding.name:
            raise WorkError("--publish refused: this session belongs to %s, it does "
                            "not publish on behalf of %s" % (binding.name, text))
        if binding.source not in identity.BOUND_SOURCES:
            raise WorkError("--publish refused: identity %s is %s, not bound (runner "
                            "lease or `ameesh mail bind`)" % (binding.name, binding.source))
        if is_auditor(cfg, db, binding.name):
            return binding.name
        raise WorkError(refus % binding.name)
    if not text:
        raise WorkError("--publish: say who publishes (--actor human:<id>)")
    if text.startswith(HUMAN_PREFIX) and len(text) > len(HUMAN_PREFIX):
        return text
    raise WorkError(
        "--publish refused: %s is not a human and this session has no bound agent "
        "identity (runner lease or `ameesh mail bind`); the auditor publishes from "
        "its own bound session, a human terminal says --actor human:<id>" % text)


def milestone_actor(cfg, db, explicit: str | None = None, *, kind: str = "") -> str:
    """#2928 : l'auteur d'un jalon `frozen`/`verdict`, vérifié au chemin d'écriture.

    Même garde que `forcer` (#540), pour la même raison : un jalon est une
    preuve de relecture, et `--actor` ne doit pas permettre à une session de
    signer à la place d'un autre agent. Une session qui porte une identité
    d'agent ne pose le jalon qu'en son propre nom (`--actor` différent :
    refus) ; une identité refusée ne retombe pas sur le terminal humain. Une
    session sans identité (un humain à son terminal) nomme un humain
    (`--actor human:<id>`) — un nom d'agent sans liaison est refusé, sinon
    n'importe quelle session poserait un verdict au nom d'un relecteur.

    La provenance exacte (#1465) est ensuite écrite dans la même transaction
    que le jalon ; un tour managé ne peut signer que sa propre preuve.
    """
    from . import identity

    text = (explicit or "").strip()
    if text and not text.startswith(HUMAN_PREFIX):
        # une graphie d'alias désigne le même agent ; le nom écrit reste le
        # primaire, comme pour l'acteur ordinaire d'une commande `work`.
        from .agent_names import resolve
        text = resolve(text, db=db, cfg=cfg, operation="work.milestone")
    binding = identity.resolve_binding(cfg, db)
    if not binding.ok and (binding.name or binding.source != "none"
                           or os.environ.get("AGENT_MAIL_NAME")):
        raise WorkError("milestone refused: invalid session identity (%s)"
                        % (binding.reason or "unbound identity"))
    if binding.name:
        if text and text != binding.name:
            raise WorkError(
                "milestone refused: this session belongs to %s, it does not post a "
                "%s milestone on behalf of %s"
                % (binding.name, kind or "work", text))
        return binding.name
    if not text:
        # aucun nom : le jalon reste anonyme (« inconnu », origine inconnue),
        # comme avant ; il ne peut pas compter pour un agent à la porte.
        return UNKNOWN_ACTOR
    if text.startswith(HUMAN_PREFIX) and len(text) > len(HUMAN_PREFIX):
        return text
    raise WorkError(
        "milestone refused: %s is not a human and this session has no bound agent "
        "identity (runner lease or `ameesh mail bind`); an agent posts a milestone "
        "from its own bound session, a human terminal says --actor human:<id>" % text)


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
    force: str | None = None,
    parent_lot: int | None = None,
    mark_item_id: int | None = None,
    mark_note: str = "",
    unique_ref: bool = False,
    external_executor: str | None = None,
    footprint=None,
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
    `unique_ref` (#2954) : une seule création par `issue_ref`, toutes états
    confondus — le verrou consultatif (repo, run) sérialise deux hôtes
    concurrents et le second reçoit `DbError` ; l'appelant relit le gagnant
    (`open_incident`).

    #540 : un agent qui a déjà un lot non démarré ne reçoit pas celui-ci : il
    va dans la file d'équipe (`queued_behind`, `queue_warning`). `force` : la
    raison d'un forçage (`--force "<raison>"`), écrite au journal avec son
    auteur ; vide, aucun forçage. L'exemption du lot d'une demande de
    décision n'est pas un paramètre : `_decision_lot`, interne, réservé à
    `decisions.ask`.

    #1193: `parent_lot` is the explicit parent of a suite lot. The blocking
    edge is written **in the same transaction** as the creation (parent still
    open), traced in the journal if the parent is already finished; an unknown
    parent is refused before any creation. No number is ever guessed from the
    title or the body.

    #3447 : `external_executor` déclare l'agent externe (ou l'humain) à qui le
    lot est réservé, sans l'assigner : L165 ne le prend pas, l'alerte et le
    débit le nomment. Le geste est celui d'un pilote (`corrector`,
    `history=False`) ; un lot ne porte pas deux exécutants (`assignee` et
    `external_executor` s'excluent).

    Canon §2 (15/10/2026): the returned item carries `route_hint` when the
    title or the source looks like a non-blocking follow-up (N, P2, P3) — the
    route to the backlog, to be shown by the caller. Nothing is ever refused:
    a human request, a blocking defect and the review-after-merge lot pass
    unchanged.

    #3744 : le corps « - » est refusé quel que soit l'appelant (comme la
    note, #3683) : c'est le marqueur de l'entrée standard, jamais un énoncé.

    #4186 : sans `footprint`, l'estimation d'intake est calculée ici, pour
    tous les chemins de création qui passent par `add` (`agent-mail
    --new-lot`, incident, empreinte §16, lot de relecture, porteur de paquet,
    `add_or_note`) et pas seulement le CLI. Sans `cfg` (aucun projet connu),
    rien n'est deviné : l'empreinte reste inconnue. `_decision_lot` appelle
    `_create` directement et n'en porte pas : une question n'occupe pas de
    fichiers."""
    if (body or "").strip() == "-":
        raise WorkError("body refused: `-` alone is not a statement; write the text, "
                        "or pass `--body -` to ameesh work add to read it on standard "
                        "input")
    from .agent_names import resolve as resolve_agent_name
    assignee = resolve_agent_name(assignee, db=db, operation="work.add")
    if footprint is None and not _is_vehicle_branch(branch):
        from . import work_footprints
        footprint = work_footprints.estimate_for_creation(
            cfg if cfg is not None else getattr(db, "cfg", None),
            app=app, title=title, body=body, branch_target=branch_target or "")
    return _create(db, title=title, type=type, source=source, app=app, body=body,
                   issue_ref=issue_ref, workstream=workstream, assignee=assignee,
                   budget_usd=budget_usd, actor=actor, package=package, externe=externe,
                   cfg=cfg, branch=branch, branch_target=branch_target, priority=priority,
                   estimate=estimate, estimate_source=estimate_source, size=size,
                   note=note, force=force, parent_lot=parent_lot,
                   mark_item_id=mark_item_id, mark_note=mark_note,
                   unique_ref=unique_ref, team_queue=True,
                   external_executor=external_executor, footprint=footprint)


#: #3209 : les états d'un lot encore ouvert, lus par `open_with_source`
OPEN_STATES = tuple(s for s in STATES if s not in TERMINAL and s not in MERGED_STATES)


def open_with_source(db: Db, source: str, *, limit: int = 1000) -> dict | None:
    """#3209 : le plus ancien lot ouvert qui porte exactement `source`, ou
    None. Lecture bornée par état : au-delà, un doublon peut s'ouvrir, jamais
    un suivi se perdre."""
    found = None
    for state in OPEN_STATES:
        for item in storage.of(db).work.items(state=state, assignee=None, limit=limit):
            if (item.get("source") or "") == source and (
                    found is None or int(item["id"]) < int(found["id"])):
                found = item
    return found


def with_source(db: Db, source: str, *, limit: int = 50) -> list[dict]:
    """#4291 : lots portant exactement `source`, tous états confondus, du plus
    récent au plus ancien. Lecture seule, bornée ; `by_source` garde son ordre
    historique (`id`) pour les empreintes."""
    rows = list(storage.of(db).work.by_source(source))
    rows.sort(key=lambda row: (str(row.get("updated_at") or ""), int(row["id"])),
              reverse=True)
    return rows[:int(limit)] if limit else rows


def update_generated(db: Db, item_id: int, *, title: str, body: str,
                     priority: int | None = None, note: str = "",
                     actor: str = SYSTEM_SENDER) -> dict | None:
    """#4291 : rafraîchit le titre, le corps et (seulement à la hausse) la
    priorité d'un lot généré encore ouvert, avec une ligne de journal.

    Une priorité plus basse (2 ou 3) ne remplace jamais une priorité plus
    haute déjà posée, par un humain notamment : la détection automatique ne
    rétrograde pas un lot. Rend le lot relu, ou None s'il n'est plus ouvert.
    """
    if not (title or "").strip():
        raise WorkError("generated title required")
    return storage.of(db).work.update_generated(
        int(item_id), title=title.strip(), body=body, priority=priority,
        note=(note or "").strip(), actor=actor)


def add_or_note(db: Db, *, source: str, occurrence: str, actor: str = "",
                **fields) -> dict:
    """#3209 : un seul lot ouvert par `source` — un suivi automatique (lot de
    correction d'un check non concluant) qui revient à chaque paquet ou
    release est noté sur le lot déjà ouvert (`occurrence`) au lieu d'en
    ouvrir un autre. Une lecture impossible ouvre le lot : mieux vaut un
    doublon qu'un suivi perdu."""
    try:
        existing = open_with_source(db, source)
    except Exception:  # noqa: BLE001 - lecture impossible : on ouvre
        existing = None
    if existing is not None:
        note(db, int(existing["id"]), occurrence, actor=actor)
        return dict(existing, noted=True)
    return add(db, source=source, actor=actor, **fields)


def _decision_lot(db: Db, *, title: str, requester: str, app: str, cfg=None) -> dict:
    """#540 : le lot de la question de `requester` (`decisions.ask`), à son
    nom même s'il a déjà un lot non démarré : il attend la réponse, ce lot
    ne va jamais dans la file d'équipe. Seul appelant : `decisions.ask`."""
    return _create(db, title=title, assignee=requester, actor=requester,
                   source="decision", app=app, cfg=cfg, team_queue=False)


def _is_vehicle_branch(branch) -> bool:
    """#4667 : la branche d'un lot est-elle celle d'un véhicule de paquet
    (`integration/**`, §11 bis) ? Un véhicule n'a pas de delta propre : il
    n'entre pas dans l'en-cours d'un agent (#540) et ne porte aucune
    empreinte d'intake (#3185)."""
    from . import packet_guard

    return bool((branch or "").strip()) and packet_guard.is_packet_branch(branch)


def _create(db: Db, *, title, type="evolution", source="", app="",  # noqa: A002
            body="", issue_ref=None, workstream=None, assignee=None, budget_usd=None,
            actor="", package=None, externe=False, cfg=None, branch=None,
            branch_target=None, priority=None, estimate=None, estimate_source=None,
            size=None, note="", force=None, parent_lot=None, mark_item_id=None,
            mark_note="", unique_ref: bool = False, team_queue: bool,
            external_executor=None, footprint=None) -> dict:
    """`add`, et `_decision_lot` (`team_queue=False`)."""
    minutes = None
    if estimate not in (None, ""):
        from . import estimates
        minutes = estimates.parse(estimate)
    if type not in ("bug", "evolution"):
        from .modules import registry as module_registry
        if not module_registry.admits(db, app or workstream or "", type):
            raise WorkError("unknown type %r for project %r: enable its module in the canon"
                            % (type, app or workstream or ""))
    if size not in (None, "") and size not in SIZES:
        raise WorkError("unknown size: %r (s, m or l)" % (size,))
    if not (title or "").strip():
        raise WorkError("title required")
    if priority is not None and priority not in PRIORITIES:
        raise WorkError("priority 1 (high), 2 (normal) or 3 (low): %r" % (priority,))
    queued = None
    if parent_lot is not None:
        try:
            parent_lot = int(parent_lot)
        except (TypeError, ValueError):
            raise WorkError("--parent-lot: a lot number, not %r" % (parent_lot,)) from None
        if parent_lot <= 0:
            raise WorkError("--parent-lot: a positive lot number")
        if get(db, parent_lot) is None:
            raise WorkError("parent lot #%d not found" % parent_lot)
    vehicle = _is_vehicle_branch(branch)
    fence = None
    if assignee:
        # L165 (EP4) : créer un lot déjà attribué est une attribution (le
        # destinataire sera réveillé) ; un lot de décision (`team_queue=False`,
        # assigné à son demandeur) n'est pas ce geste. La garde ne fait que
        # lire ici : `hold` et l'écriture sont dans la même transaction plus
        # bas, sinon une prise du rôle entre `check_assignee` et `work.add`
        # laisserait créer sous l'ancienne époque (revue cx2 B3).
        if team_queue:
            from . import pilot_gesture
            fence = pilot_gesture.gate(cfg, db, "assign",
                                       "add for %s" % assignee, project=(app or None))
        # L46 : l'assigné enregistré est celui que la garde a normalisé
        check = check_assignee(db, assignee, externe=externe, cfg=cfg)
        assignee = check["assignee"]
        # #4667 : un véhicule de paquet n'est pas un lot de travail : il ne
        # prend pas la place du seul lot non démarré de l'agent (#540).
        queued = check if team_queue and not vehicle else None
    elif externe:
        raise WorkError("--externe without assignee: nothing to force")
    external_executor = (external_executor or "").strip() or None
    if external_executor:
        if assignee:
            raise WorkError("a lot carries one executor: %s is assigned, drop the "
                            "external executor (or assign it instead)" % assignee)
        # #3447 : déclarer l'exécutant externe est un geste de pilote. Le lot
        # reste sans assigné (l'agent externe ne se réveille pas, 0030).
        actor = corrector(cfg, db, actor, what="external executor", history=False)
        external_executor = check_external_executor(db, external_executor, cfg=cfg)
    fiche = _package(db, package) if package else None
    branch, branch_target = _branch_args(branch, branch_target)
    if _is_vehicle_branch(branch):
        # #4667 : le corps d'un porteur cite souvent le script qui le compose ;
        # une empreinte d'intake inférée de ce texte est fausse (le véhicule
        # n'a pas de delta propre) et refusait son attribution (#3185).
        footprint = []
    created = ("created" + (" (plan: %s)" % package if package else "")
               + ("; estimate: %s" % span(minutes * 60) if minutes else "")
               + ("; reserved to external executor %s" % external_executor
                  if external_executor else "")
               + ("; %s" % note if note else ""))


    def create(tx):
        return storage.of(tx).work.add(
            type=type, source=source, app=app, title=title.strip(), body=body,
            issue_ref=issue_ref, workstream=workstream, assignee=assignee,
            budget_usd=budget_usd, note=created,
            actor=actor, package_id=fiche["id"] if fiche else None,
            package_parent=fiche.get("parent") if fiche else None,
            branch=branch, branch_target=branch_target, priority=priority,
            estimate_minutes=minutes,
            estimate_source=(" ".join(str(estimate_source or "").split()) or None)
            if minutes else None, change_size=size or None,
            queued_note=queued and _queued_note(queued, created),
            forced_note=queued and _forced_note(queued, created, force, actor),
            mark_item_id=mark_item_id, mark_note=mark_note, parent_lot=parent_lot,
            unique_ref=unique_ref, external_executor=external_executor,
            # #4173 : l'empreinte d'intake est écrite par la création même ;
            # attribuée, celle-ci est refusée si elle chevauche un lot en cours
            # (`FootprintOverlap`, rien n'est écrit).
            **({"footprint": list(footprint)} if footprint else {}))

    if fence is None:
        item = create(db)
    else:
        with db.transaction() as tx:
            fence.hold(tx)
            item = create(tx)

    if item.get("queued_behind") is not None:
        item["queue_warning"] = queue_warning(db, item, assignee)
    # L317c : un bug de priorité 1 ouvert par un humain (ou son chat) réveille
    # l'auditeur pour une analyse profonde, tout de suite
    from . import deep_analysis
    analysis = deep_analysis.on_lot_created(db, item, actor=actor, cfg=cfg)
    if analysis is not None:
        item["deep_analysis"] = analysis
    # canon §2 (15/10/2026): a review follow-up (N, P2, P3) does not create a
    # lot, it goes to the backlog. The warning is produced by the creation
    # itself (every entry point gets it); nothing is refused.
    from . import backlog
    hint = backlog.followup_hint(title=item.get("title") or title, source=source,
                                 issue_ref=issue_ref or "", app=app or "")
    if hint:
        item["route_hint"] = hint
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
               actor: str = "", source: str = "", clear_target: bool = False) -> dict:
    """L118 : la branche (et la cible) d'un lot ouvert ; journalisée. Rend
    le lot. Une branche identique (et la même cible) ne réécrit rien.
    `clear_target` (#2931) efface la cible explicite — le lot retombe sur la
    cible de son projet ou du dépôt ; `target=None` la conserve."""
    branch, target = _branch_args(branch, target)
    if not branch:
        raise WorkError("empty branch")
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    same = (item.get("branch") == branch
            and ((clear_target and not item.get("branch_target"))
                 or (not clear_target
                     and (target is None or item.get("branch_target") == target))))
    if same:
        return item
    if clear_target:
        target = None
    else:
        target = target if target is not None else (
            item.get("branch_target") if item.get("branch") == branch else None)
    note = "branch %s%s%s (before: %s)" % (
        branch,
        " to %s" % target if target else (
            " (target cleared)" if clear_target else ""),
        " — %s" % source if source else "", item.get("branch") or "none")
    row = storage.of(db).work.set_branch(item_id, branch, target, note=note, actor=actor)
    if row is None:
        raise WorkError("lot %s already %s: branch not set" % (item_id, item["state"]))
    return row


#: #2102 : le début de la ligne de journal d'un détachement, suivi du nom
#: court de la branche et d'une espace ; le relevé des fusions n'associe plus
#: implicitement (L118b) une branche ainsi détachée
DETACH_NOTE = "branch detached: "


def _detach_prefix(branch: str) -> str:
    from . import plan_git

    return "%s%s " % (DETACH_NOTE, plan_git.short_branch(branch))


def is_detached(st, item_id: int, branch: str) -> bool:
    """#2102 : la branche `branch` a été détachée du lot par `detach_branch`
    (une ligne de journal la nomme). `st` : le stockage (`storage.of`)."""
    prefix = _detach_prefix(branch)
    return any((e.get("note") or "").startswith(prefix)
               for e in st.work.assignment_events(int(item_id), [prefix]))


def attach_implicit_branch(db: Db, item_id: int, branch: str, *, source: str,
                           actor: str = "") -> dict | None:
    """L118b, #2102 : la branche implicite `lot/<id>-…` devient celle du lot,
    journalisée, seulement si le lot n'a toujours pas de branche et qu'elle
    n'en a pas été détachée — vérifié atomiquement par le stockage, pour
    qu'un détachement ou une branche explicite posés pendant le relevé ne
    soient pas écrasés. Rend le lot, ou None si rien n'est posé."""
    note = "branch %s — %s (before: none)" % (branch, source)
    return storage.of(db).work.attach_implicit_branch(
        item_id, branch, detached_prefix=_detach_prefix(branch), note=note, actor=actor)


def detach_branch(db: Db, item_id: int, branch: str | None = None, *, reason: str,
                  actor: str = "") -> dict:
    """#2102 : retire la branche d'un lot ouvert (PR fermée ou supplantée),
    sans toucher à son état, son titulaire ni son relecteur ; journalisé avec
    la raison et le dernier commit vu. Git n'est pas touché : la branche et
    ses commits restent là où ils sont. Le relevé des fusions ne réassocie
    plus implicitement ce nom ; une réassociation se fait explicitement
    (`set_branch`, `ameesh work branch <id> <branche>`).

    `branch` : la branche à détacher, par défaut celle du lot ; un lot sans
    branche peut nommer celle à ne plus associer (détachement fait hors CLI).
    Un nom déjà détaché d'un lot sans branche ne réécrit rien. Rend le lot."""
    reason = (reason or "").strip()
    if not reason:
        raise WorkError("a detachment needs a reason (--reason)")
    named, _ = _branch_args(branch, None)
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    from . import plan_git

    current = item.get("branch") or None
    name = named or current
    if not name:
        raise WorkError("lot %s has no branch: name the branch to detach" % item_id)
    if current and plan_git.short_branch(name) != plan_git.short_branch(current):
        raise WorkError("lot %s carries the branch %s, not %s" % (item_id, current, name))
    st = storage.of(db)
    if not current and is_detached(st, item_id, name):
        return item
    head = (item.get("branch_head") or "").strip()
    note = "%s(%s%s) — %s; the merge sweep no longer associates it implicitly " \
           "(to associate it again: ameesh work branch %d %s)" % (
               _detach_prefix(name),
               "last seen %s" % head[:12] if head else "no commit seen",
               ", target %s" % item["branch_target"] if item.get("branch_target") else "",
               reason, int(item_id), plan_git.short_branch(name))
    row = st.work.detach_branch(item_id, current, note=note, actor=actor)
    if row is None:
        now = get(db, item_id) or item
        if now["state"] in MERGED_STATES or now["state"] == lifecycle.CLOSED:
            raise WorkError("lot %s already %s: branch not detached" % (item_id, now["state"]))
        raise WorkError("lot %s: its branch changed meanwhile (now %s): nothing detached, "
                        "read the lot again" % (item_id, now.get("branch") or "none"))
    return row


# --------------------------------------------------------------------------
# numéro de migration réservé (L122)
# --------------------------------------------------------------------------

#: le dépôt d'un lot, lu dans son issue : `owner/repo#N`
_ISSUE_REPO_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#[0-9]+$")
#: #2603 : une PR fusionnée hors du dépôt du lot, `owner/repo#N`
_PR_REF_RE = re.compile(r"^(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[0-9]+)$")
#: #3885 : le dépôt d'une réservation de migration, quand il diffère de l'issue
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


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


def _migration_target(item: dict, checkout: str, cfg=None) -> str:
    """La cible du lot pour l'invariant des numéros de migration (#2620) :
    `branch_target`, sinon la cible de son projet, sinon celle que le clone
    déclare (`git config ameesh.target`) — jamais sa branche par défaut
    (`origin/HEAD`), qui nomme main sur un clone nexlink alors que le projet
    intègre sur develop (#2660). Chaîne vide quand aucune n'est déclarée :
    l'appelant refuse alors explicitement."""
    from . import lot_class

    return str(lot_class.default_base(cfg, item, checkout, declared_only=True) or "").strip()


def _short_repo(repo) -> str:
    """#2481 : le dépôt d'un créneau est stocké `github.com/owner/repo` ; la
    vue et `work.migration_line` gardent la forme `owner/repo` du scalaire."""
    text = str(repo or "")
    prefix = "github.com/"
    return text[len(prefix):] if text.startswith(prefix) else text


def reserved_migrations(item: dict) -> list[dict]:
    """#2481 : les réservations d'un lot, liste ordonnée `{number, repo, slot}`.

    `migrations` vient de `item_columns` (table `work_migration_reservations`) ;
    une base d'avant 0419 ou un client ancien n'a que le scalaire, qui est
    alors repris. La réservation principale reste le scalaire ; ce helper ne
    déduplique que les doublons exacts (même numéro, même dépôt)."""
    rows, seen = [], set()
    for raw in item.get("migrations") or ():
        if not isinstance(raw, dict):
            continue
        try:
            number = int(raw.get("number"))
        except (TypeError, ValueError):
            continue
        repo = _short_repo(raw.get("repo") or item.get("migration_repo"))
        key = (number, repo)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"number": number, "repo": repo,
                     "slot": str(raw.get("slot") or "default")})
    if item.get("migration") is not None:
        number = int(item["migration"])
        repo = _short_repo(item.get("migration_repo"))
        if (number, repo) not in seen:
            rows.append({"number": number, "repo": repo, "slot": "default"})
    rows.sort(key=lambda row: (row["number"], row["slot"]))
    return rows


def reserve_migration(db: Db, item_id: int, number: int | None = None, *,
                      release: bool = False, repo: str | None = None,
                      actor: str = "", cfg=None,
                      skip_branches: tuple[str, ...] = (),
                      skip_worktree: bool = False,
                      target: str | None = None,
                      repository: str | None = None) -> dict:
    """L122, #443, #2481, #2620 : réserve au lot le numéro de sa migration,
    avant d'écrire le fichier, dans le dépôt du lot (`lot_repository`) : le
    dossier et la numérotation sont les siens, jamais ceux d'ameesh pour un
    autre dépôt. `repo` : un clone local de ce dépôt (défaut : celui que
    l'hôte déclare, sinon le dépôt git courant) ; son `origin` doit être le
    dépôt du lot. Sans `number` : le prochain libre, au-delà des numéros du
    dépôt (arbre, toutes ses branches) et de ceux déjà réservés pour ce dépôt.
    `number` est refusé s'il existe déjà **sur la branche cible du lot** —
    `branch_target`, sinon la cible de son projet, sinon celle que le clone
    déclare (`git config ameesh.target`), `origin/` publiée d'abord ; sans
    cible déclarée, refus — jamais la branche par défaut `origin/HEAD`, qui
    peut nommer main alors que le projet intègre sur develop (#2660) ;
    l'arbre de travail, le paquet installé
    et les autres branches ne comptent pas ; une branche non fusionnée sera
    renumérotée à sa fusion, #2400 — ou s'il est réservé par un autre lot
    (index uniques (dépôt, numéro) : la base tranche, même entre deux
    réservations simultanées). Un lot qui a déjà une réservation garde les
    précédentes : un second numéro s'ajoute (liste `migrations`), il ne
    remplace plus la première (#2481). `release` libère toutes les
    réservations, ou seulement `number`, y compris sur un lot déjà terminal
    (#956 : un lot fusionné gardait sa réservation et bloquait le numéro).
    `skip_branches` (#1603) : branches supplémentaires ignorées par le scan du
    prochain libre, en plus de celle du lot — la branche d'intégration d'un
    paquet en composition, dont les fichiers renumérotés ne doivent pas compter
    contre la réservation. `skip_worktree` (#2400) : ignorer l'arbre de travail
    du clone de lecture — le train y compose justement la migration à réserver.
    `target` (#2620) : la cible à lire pour un numéro exact quand l'appelant la
    connaît déjà (le train compose une cible explicite), sinon la cible
    déclarée du lot (#2660).
    `repository` (#3885) : le dépôt dont l'espace de numérotation est réservé,
    quand le code vit dans un autre dépôt que l'issue du lot (un lot ameesh
    dont le ticket miroir vit dans Nexlink). Sans lui, le dépôt reste celui de
    l'issue (`lot_repository`). Le clone passé par `--repo` doit être celui de
    ce dépôt.
    Rend le lot, avec `migration_file` et la liste `migrations`. Une correction
    concurrente de `issue_ref` pendant le scan du dépôt est refusée (#1830 :
    `issue reference changed meanwhile, retry`), jamais écrite en silence."""
    from . import migrations

    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if item["state"] in lifecycle.COMPLETED and not release:
        raise WorkError("lot %s already %s: no reservation" % (item_id, item["state"]))
    reservations = reserved_migrations(item)
    current = item.get("migration")
    if release:
        if not reservations:
            return _with_file(item, cfg)
        numbers = {row["number"] for row in reservations}
        target = None if number is None else int(number)
        if target is not None and target not in numbers:
            raise WorkError("lot %s holds no migration %s (%s): nothing released" % (
                item_id,
                migration_layout(item, cfg, repo=reservations[0]["repo"]).label(target),
                ", ".join(migration_layout(item, cfg, repo=row["repo"]).label(row["number"])
                          for row in reservations)))
        reserved_repo = reservations[0]["repo"] or item.get("migration_repo")
        layout = migration_layout(item, cfg, repo=reserved_repo)
        if target is not None:
            note = "migration %s released (%s)" % (layout.label(target), layout.repo)
        elif len(reservations) == 1:
            note = "migration %s released (%s)" % (
                layout.label(reservations[0]["number"]), layout.repo)
        else:
            note = "migrations %s released (%s)" % (
                ", ".join(layout.label(row["number"]) for row in reservations), layout.repo)
        row = storage.of(db).work.reserve_migration(
            item_id, reserved_repo, None, 0, actor=actor, release=True,
            release_number=target, issue_ref=item.get("issue_ref"), note=note)
        if row is None:
            raise WorkError("lot %s: migration reservation changed meanwhile, retry" % item_id)
        return _with_file(row, cfg)
    if current is None and any(row["slot"] != "default" and row["slot"] != "legacy-%d" % row["number"]
                               for row in reservations):
        raise WorkError("named migration slots require --slot; legacy allocation refused")
    if number is not None and not 1 <= int(number) <= 9999:
        raise WorkError("migration number outside 0001-9999: %s" % number)
    held = {row["number"] for row in reservations}
    if number is None:
        if reservations:
            return _with_file(item, cfg)
    else:
        number = int(number)
        if number in held:
            return _with_file(item, cfg)
    if repository:
        # #3885 : le code peut vivre dans un autre dépôt que l'issue (un lot
        # ameesh dont le ticket miroir est dans Nexlink). L'option est
        # explicite : jamais de repli deviné.
        repository = (repository or "").strip().lower()
        if not _REPO_SLUG_RE.match(repository):
            raise WorkError("--repository: expected owner/repo, not %r" % repository)
        slug = migrations.repo_key(repository)
    else:
        slug = lot_repository(item, cfg)
    layout = migrations.layout_for(slug, cfg)
    if layout is None:
        raise WorkError("repository %s: migrations directory unknown (declare it in the host "
                        "configuration: radar.projects.<project> with github %s and "
                        "migrations <dir>)" % (slug, slug))
    checkout = _checkout(slug, repo, cfg)
    trunks = sorted(set(skip_branches) & migrations.trunk_branches(checkout))
    if trunks:
        # ignorer le tronc cacherait les migrations déjà fusionnées (#1603)
        raise WorkError("--skip-branch %s: a trunk branch is always scanned" % trunks[0])
    if number is None:
        present = migrations.repo_numbers(checkout, layout, skip_branch=item.get("branch"),
                                          also_skip=tuple(skip_branches),
                                          worktree=not skip_worktree)
        floor = max(present or [0]) + 1
        note = "migration %%(number)0%dd reserved (%s)" % (layout.width, slug)
    else:
        number = int(number)
        # #2620 : un numéro exact se lit sur la cible seule. Une branche non
        # fusionnée qui porte le même numéro ne l'a pas fait atterrir : elle
        # sera renumérotée à sa fusion (#2400), elle ne bloque pas ce lot.
        target = (target or "").strip() or _migration_target(item, checkout, cfg)
        present = migrations.target_numbers(checkout, layout, target)
        if present is None:
            if target:
                detail = "target branch %r of lot #%s is unknown in %s" % (
                    target, item_id, checkout)
                hint = "fetch it (`git fetch origin`) or name the target"
            else:
                detail = "no target branch is declared for lot #%s (checkout %s)" % (
                    item_id, checkout)
                hint = ("the clone's default branch (`origin/HEAD`) is never read for an "
                        "exact number (#2660); name the target")
            raise WorkError(
                "migration %s: %s — an exact number is checked against the target branch "
                "only (#2620); %s (`ameesh work branch %s <branch> --target <target>`, or "
                "`git config ameesh.target <target>`)"
                % (layout.label(number), detail, hint, item_id))
        if number in present:
            raise WorkError("migration %s already in %s (%s): choose another number" % (
                layout.label(number), slug, ", ".join(sorted(present[number])[:3])))
        floor = 0
        note = "migration %s reserved (%s; before: %s)" % (
            layout.label(number), slug,
            layout.label(current) if current is not None else "none")
    try:
        row = storage.of(db).work.reserve_migration(item_id, slug, number, floor, note=note,
                                                    actor=actor,
                                                    issue_ref=item.get("issue_ref"))
    except storage.ReservationContention as exc:
        # #478 : budget de réessais épuisé sous la rafale — message lisible,
        # jamais la DbError brute de l'index unique
        raise WorkError("reservation contention, retry") from exc
    except DbError as exc:
        # une autre erreur qui nomme l'index (permission refusée) n'est pas
        # une réservation concurrente : elle remonte telle quelle (codex #478 r1)
        indexes = (_pg.MIGRATION_INDEX, _pg.MIGRATION_SLOTS_INDEX)
        if number is None or not any(is_unique_violation(exc, index)
                                      for index in indexes):
            raise
        holder = storage.of(db).work.migration_holder(number, slug)
        raise WorkError("migration %s of %s already reserved by lot #%s (%s)" % (
            layout.label(number), slug, holder["id"] if holder else "?",
            holder["title"] if holder else "concurrent reservation")) from exc
    if row is None:
        # #1830 : le CAS a échoué ; si le lot est encore ouvert et que sa
        # référence d'issue a bougé pendant le scan du dépôt, le dire — sinon
        # c'est la fermeture du lot qui a refusé.
        again = get(db, item_id)
        if (again is not None and again["state"] not in lifecycle.COMPLETED
                and (again.get("issue_ref") or "") != (item.get("issue_ref") or "")):
            raise WorkError("lot %s: issue reference changed meanwhile, retry" % item_id)
        raise WorkError("lot %s is no longer open: no reservation" % item_id)
    return _with_file(row, cfg)


def migration_layout(item: dict, cfg=None, repo: str | None = None):
    """La disposition des migrations du dépôt réservé (défaut : 4 chiffres).
    `repo` (#1567) : le dépôt du numéro **atterri**, quand il diffère de la
    réservation (celle-ci peut avoir été libérée depuis, #956)."""
    from . import migrations

    slug = repo or item.get("migration_repo") or ""
    return migrations.layout_for(slug, cfg) or migrations.Layout(slug, "", 4)


def _with_file(item: dict, cfg=None) -> dict:
    """#2481 : ajoute les libellés et fichiers de toutes les réservations."""
    reservations = reserved_migrations(item)
    if item.get("migration") is None:
        return dict(item, migrations=reservations)
    layout = migration_layout(item, cfg)
    enriched = []
    for row in reservations:
        row_layout = migration_layout({}, cfg, repo=row["repo"])
        enriched.append(dict(
            row,
            label=row_layout.label(row["number"]),
            file=(row_layout.file(row["number"]) if row_layout.directory else None)))
    return dict(item, migration_label=layout.label(item["migration"]),
                migration_file=layout.file(item["migration"]) if layout.directory else None,
                migrations=enriched)


def _migration_file(number: int, repo: str, cfg=None) -> str:
    """Le fichier attendu d'un numéro de migration de `repo` (`<name>` est le
    nom que l'auteur donne au fichier)."""
    layout = migration_layout({}, cfg, repo=repo)
    return (layout.file(number) if layout.directory
            else "<migrations>/%s_<name>.sql" % layout.label(number))


def migration_line(item: dict, cfg=None) -> str | None:
    """#1567, #2481 : la ligne `migration:` d'un lot. Le numéro **atterri** est
    nommé quand il est enregistré (avec son fichier, et la ou les réservations
    d'origine quand elles en diffèrent). Sinon la liste des réservations est
    nommée (un lot peut en tenir plusieurs), et pour un lot terminal,
    « landed number not recorded » : la vue ne fait jamais passer la
    réservation pour le fichier atterri."""
    landed = item.get("migration_landed")
    reservations = reserved_migrations(item)
    if landed is not None:
        repo = item.get("migration_landed_repo") or item.get("migration_repo")
        line = "%s landed in %s (L122) — file %s" % (
            migration_layout(item, cfg, repo=repo).label(landed), repo,
            _migration_file(landed, repo, cfg))
        others = [row for row in reservations if row["number"] != int(landed)]
        if others:
            line += " (reserved %s)" % ", ".join(
                migration_layout({}, cfg, repo=row["repo"]).label(row["number"])
                for row in others)
        return line
    if not reservations:
        return None
    if len(reservations) == 1:
        row = reservations[0]
        line = "%s reserved in %s (L122)" % (
            migration_layout({}, cfg, repo=row["repo"]).label(row["number"]),
            row["repo"] or item.get("migration_repo"))
        if item.get("state") in MERGED_STATES:
            line += " — landed number not recorded"
        return line
    labels = [migration_layout({}, cfg, repo=row["repo"]).label(row["number"])
              for row in reservations]
    repos = ", ".join(sorted({row["repo"] for row in reservations if row["repo"]}))
    line = "migrations %s reserved in %s (L122)" % (", ".join(labels), repos)
    if item.get("state") in MERGED_STATES:
        line += " — landed numbers not recorded"
    return line


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


def set_issue_ref(db: Db, item_id: int, ref: str | None, *, actor: str = "", cfg=None) -> dict:
    """#1603 : corrige la référence d'issue d'un lot, ou l'efface (`ref=None`).

    Réservé aux correcteurs (humain, orchestrateur ou agent de conception),
    comme `work set-app` : c'est `issue_ref` qui décide du dépôt où
    `work migration` réserve (`lot_repository`), un lot créé sur l'issue d'un
    autre dépôt ne peut donc pas réserver chez lui sans cette correction.
    Refusée tant que le lot garde une réservation de migration dans un autre
    dépôt que celui de la nouvelle référence (la libérer d'abord).
    L'écriture passe par le compare-and-set du storage (aucune correction
    concurrente n'est écrasée) puis laisse une ligne de journal."""
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    clean = (ref or "").strip() or None
    if clean is not None and not _ISSUE_REPO_RE.match(clean):
        raise WorkError("issue reference %r: expected owner/repo#N (or --none)" % clean)
    actor = corrector(cfg, db, actor)
    current = (item.get("issue_ref") or "").strip() or None
    if current == clean:
        return item
    reservations = reserved_migrations(item)
    if reservations:
        # les réservations restent dans le dépôt où elles ont été prises : les
        # garder sous une issue d'un autre dépôt les rendrait fausses en
        # silence (`work migration` est idempotent sur une réservation
        # existante)
        try:
            target = lot_repository(dict(item, issue_ref=clean), cfg)
        except WorkError:
            target = None
        if any(row["repo"] != target for row in reservations):
            labels = ", ".join(
                migration_layout({}, cfg, repo=row["repo"]).label(row["number"])
                for row in reservations)
            repos = ", ".join(sorted({row["repo"] for row in reservations}))
            what = "migration" if len(reservations) == 1 else "migrations"
            pronoun = "it" if len(reservations) == 1 else "them"
            raise WorkError("lot %s holds %s %s in %s: release %s first "
                            "(`ameesh work migration %s --release`), then correct the "
                            "issue reference and reserve again" % (
                                item_id, what, labels, repos, pronoun, item_id))
    if not storage.of(db).work.set_issue_ref(item_id, clean, current=item.get("issue_ref"),
                                             migration_repo=item.get("migration_repo")):
        # #1654 : le CAS couvre aussi la réservation lue ; si l'issue n'a pas
        # bougé, c'est une réservation arrivée entre la lecture et l'écriture.
        again = get(db, item_id)
        if again is not None and ((again.get("issue_ref") or "").strip() or None) == current:
            raise WorkError("lot %s: migration reservation changed meanwhile, retry" % item_id)
        raise WorkError("lot %s: issue reference changed meanwhile, retry" % item_id)
    note(db, item_id, "issue_ref %s → %s" % (current or "—", clean or "—"), actor=actor)
    return get(db, item_id) or item


def set_type(db: Db, item_id: int, type: str, *, actor: str = "", cfg=None) -> dict:  # noqa: A002
    """#2954 : corrige le type d'un lot (`bug` ↔ `evolution`).

    Un lot mal typé à sa création (cas de #2988, `bug` au lieu d'`evolution`)
    se corrige par un geste journalé, comme `work set-app` et `work
    issue-ref` : réservé aux correcteurs (humain, orchestrateur ou agent de
    conception), compare-and-set sur le type lu, ligne de journal
    `type 'bug' → 'evolution'`. Le tableau de référence des estimations et le
    libellé du ticket miroir suivent à la prochaine lecture/projection : ils
    lisent le type du lot, jamais une copie.

    `improvement` (panier) reste possible comme type **lu** — on peut en
    sortir — mais pas comme cible : la contrainte 0049 exige
    `expected_value` et `value_score`, que la file d'amélioration pose à la
    création.
    """
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if type not in RETYPE_TARGETS:
        raise WorkError("unknown target type %r: %s (an `improvement` basket is "
                        "created by the backlog with its value fields)"
                        % (type, ", ".join(RETYPE_TARGETS)))
    actor = corrector(cfg, db, actor, what="type correction")
    current = str(item.get("type") or "")
    if current == type:
        return item
    updated = storage.of(db).work.set_type(item_id, type, current=current, actor=actor)
    if updated is None:
        raise WorkError("lot %s: type changed meanwhile, retry" % item_id)
    return updated


def set_priority(db: Db, item_id: int, priority: int, *, reason: str = "",
                 actor: str = "", cfg=None) -> dict:
    """#3370 : change la priorité d'un lot **ouvert**, avec une ligne de journal.

    Un lot mal classé (cas de #3358, P1 alors que le pilote le juge non
    urgent) se corrige sans le recréer : `work set-priority <lot> 1|2|3
    --reason "<motif>"`. Même garde que `set-type` et `set-app` (humain,
    orchestrateur ou agent de conception ; une session liée agit en son nom
    et ne peut pas usurper un autre acteur). Le motif est obligatoire : une
    priorité décide des escalades §9, elle ne change pas sans dire pourquoi.

    Compare-and-set sur la priorité lue (NULL — « sans priorité » — inclus) :
    deux corrections concurrentes ne s'écrasent pas. L'état **ouvert** est
    revérifié sous le même verrou de ligne que l'écriture : une promotion,
    fusion ou fermeture intercalée ne peut pas recevoir la priorité.
    Répéter la même priorité n'écrit rien. La ligne de journal est
    `priority 1 → 2 — <motif>`.
    Seuls les lots ouverts se corrigent : un lot fusionné, promu ou fermé
    n'a plus d'escalade à régler.

    Les lecteurs (file d'attente, planification, alertes §9, étiquette
    `ameesh:pN` du ticket miroir) lisent la priorité du lot, jamais une
    copie : ils suivent à la prochaine lecture/projection.
    """
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if type(priority) is not int or priority not in PRIORITIES:
        raise WorkError("priority 1 (high), 2 (normal) or 3 (low): %r" % (priority,))
    actor = corrector(cfg, db, actor, what="priority correction")
    if item["state"] not in OPEN_STATES:
        raise WorkError("lot %s is %s: priority changes on an open lot only"
                        % (item_id, item["state"]))
    reason = " ".join((reason or "").split())
    if not reason:
        raise WorkError("a priority change needs a reason (--reason)")
    current = item.get("priority")
    if current == priority:
        return item
    updated = storage.of(db).work.set_priority(
        item_id, priority, current=current, reason=reason, actor=actor)
    if updated is None:
        # #3370 (relecture cx2) : le stockage refuse sous le verrou de ligne un
        # lot qui n'est plus ouvert. Une transition intercalée entre la
        # lecture ci-dessus et l'écriture ne doit pas écrire une priorité sur
        # un lot promu, fusionné ou fermé : on relit pour dire pourquoi.
        again = get(db, item_id)
        if again is not None and again.get("priority") == current \
                and again["state"] not in OPEN_STATES:
            raise WorkError("lot %s is %s: priority changes on an open lot only"
                            % (item_id, again["state"]))
        raise WorkError("lot %s: priority changed meanwhile, retry" % item_id)
    return updated


def open_lots(db: Db, assignee: str) -> list[dict]:
    """L118 : les lots ouverts d'un assigné (ni livrés, ni fermés)."""
    from .agent_names import resolve as resolve_agent_name
    assignee = resolve_agent_name(assignee, db=db, operation="work.open_lots")
    return storage.of(db).work.open_for(assignee)


#: #753 (B1, R2 Claude) : seuls les arrêts DÉLIBÉRÉS rendent les lots. Un arrêt
#: automatique et réversible (`erreur` de `stop_after_failures`, `retire_du_canon`
#: que `canon.revive` annule, `bail_expire`, `externe`) laisse l'agent reprendre
#: ses lots à la reprise : sinon une panne fournisseur dessaisirait en rafale
#: tous ses agents. Le pilote peut y ajouter `retire_du_canon` s'il le juge
#: délibéré.
RELEASABLE_STOP_REASONS = ("manuel",)


def release_lots(db: Db, agent: str, *, reason: str, actor: str = "",
                 measure_actor: str = "", measure_reason: str = "",
                 dry_run: bool = False,
                 stopped_with: tuple[str, ...] = ()) -> list[dict]:
    """#753 : à l'arrêt d'un agent, ses lots ouverts retournent à la file
    d'équipe — assigné effacé (assignee = NULL), journal au lot avec la raison,
    délégation en cours annulée —, et ses lots promus (un déploiement porte
    toujours sa mesure post-déploiement à refaire) vont à `measure_actor`
    (l'auditeur), avec `measure_reason`. Rend les lots rendus ; idempotent (un
    lot repris ou fermé entre-temps n'est pas touché). `dry_run` : ce qui
    serait fait, rien n'est écrit. `stopped_with` : si non vide, la transaction
    revérifie que l'agent est encore `stopped` pour l'une de ces raisons avant
    de rendre un lot — sans quoi une reprise (`ameesh resume`) entre le relevé
    du registre et le geste dessaisirait un agent qui travaille de nouveau."""
    name = (agent or "").strip()
    if not name:
        return []
    return storage.of(db).work.release_agent(
        name, reason=reason, actor=actor, measure_actor=measure_actor,
        measure_reason=measure_reason, dry_run=dry_run,
        stopped_with=stopped_with)


def held_lots(db: Db, agents) -> dict[str, dict]:
    """#2877 : ce que ces agents tiennent encore, en une lecture sans verrou —
    par nom, `open` (un lot ouvert) et `promoted` (un lot promu) ; un agent qui
    ne tient rien est absent. `release_lots` verrouille la ligne du registre de
    l'agent : on ne l'appelle que s'il y a quelque chose à rendre."""
    names = [(name or "").strip() for name in agents]
    return storage.of(db).work.held_by([name for name in names if name])


def measurement_actor(db: Db, cfg=None, *, listing=None, canons=None) -> str:
    """#753 : l'acteur de la mesure post-déploiement d'un lot promu —
    l'auditeur du registre (l'agent `auditeur`, ou celui dont la fiche Agent
    du canon porte le rôle). Un auditeur arrêté ou mort n'est pas un acteur :
    on ne réassigne pas un lot à un agent arrêté, ce serait le mal que ce lot
    corrige. Chaîne vide s'il n'y a aucun auditeur vivant, ou plusieurs sans
    choix évident : les lots promus restent alors en place. Jamais
    d'exception. `canons` : canons déjà chargés (le balayage de l'exécuteur
    espace cette lecture, #1425 N5) ; `None` les lit ici."""
    from . import alert_briefs, registry

    try:
        if listing is None:
            listing = registry.overview(db)
    except Exception:  # noqa: BLE001 - jamais fatal à l'arrêt d'un agent
        return ""
    living = {row.get("name") for row in listing
              if row.get("name")
              and (row.get("status") or "") not in ("stopped", "dead")}
    if alert_briefs.AUDITOR_NAME in living:   # le nom suffit : pas de canon
        return alert_briefs.AUDITOR_NAME
    names = [name for name in alert_briefs.auditors(
                 listing, configured_canons(cfg) if canons is None else canons)
             if name in living]
    return names[0] if len(names) == 1 else ""


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


#: #1979 : marque « verdict non requis » que le pilote pose dans la colonne du
#: relecteur quand aucun re-verdict n'est dû (résolution de composition
#: mécanique, canon §11 bis/#87, ou verdict conservé d'une tête relue) : sans
#: elle, le registre affiche un faux « waiting: verdict » et les rappels
#: partent à un relecteur qui n'a rien à relire. Le jeton commence par un
#: caractère que `config.NAME_RE` refuse : il ne peut pas désigner un agent.
REVIEWER_NONE = _pg.REVIEWER_NONE


def check_reviewer(db: Db, reviewer: str, *, cfg=None) -> str:
    """#705 : le relecteur désigné d'une revue est un nom joignable.

    Un agent du registre, ou un humain (`human:…`, ou un nom nu qui en désigne
    un, normalisé comme pour l'assignation). Un nom inconnu est refusé : la
    revue attendrait quelqu'un que ni le courrier ni l'alerte ne peuvent
    joindre — le défaut que ce lot corrige. Un agent arrêté reste accepté : il
    porte l'attente et sera relancé à sa reprise. Un agent retiré (#3460) est
    refusé : il ne reviendra pas lire la demande.
    """
    from . import registry

    name = (reviewer or "").strip()
    if not name:
        raise WorkError("empty reviewer")
    if name.startswith(HUMAN_PREFIX):
        return name
    # #423 : un alias se résout au nom primaire, comme l'assigné — sinon la
    # règle « le relecteur n'est pas l'assigné » compare deux orthographes.
    row = registry.get(db, name)
    if row is not None:
        if isinstance(row, dict):
            if row.get("retired_ts"):
                raise WorkError("reviewer refused: %s is retired (ameesh agent retire, "
                                "#3460): name an active reviewer"
                                % (row.get("name") or name))
            # #4779 : la fiche doit prouver la capacité `propose` et un rôle.
            # Un canari `[read]` / `roles: []` retenait la porte sans pouvoir
            # rendre d'avis ; une capacité absente du registre est un doute,
            # pas une preuve, et la désignation est refusée de même.
            from . import review_designation
            eligible, reason = review_designation.reviewer_eligibility(row)
            if not eligible:
                raise WorkError(
                    "reviewer refused: %s cannot review (canon §2, #4779): %s; "
                    "a reviewer's card must carry the `propose` capacity and at "
                    "least one role" % (
                        row.get("name") or name,
                        review_designation.PROFILE_REASONS.get(reason, reason)))
            return row.get("name") or name
        # Doublure de registre (tests #705 r2 N2) : pas un mapping, on garde
        # l'orthographe demandée, comme avant #423.
        return name
    cfg = cfg if cfg is not None else getattr(db, "cfg", None)
    human = known_human(db, name, cfg)
    if human:
        return human
    raise WorkError("reviewer refused: %s is not in the registry (unknown agent): "
                    "the review would wait for nobody; register it "
                    "(agent-runner register …), or name a human (human:<id>)" % name)


def check_reviewer_choice(db: Db, item: dict, reviewer: str, *, cfg=None) -> str:
    """#705 : le relecteur désigné est joignable et relit un AUTRE lot.

    L'assigné du lot ne peut pas être son relecteur : le verdict attendu vient
    d'ailleurs, sinon la désignation ne corrige rien."""
    name = check_reviewer(db, reviewer, cfg=cfg)
    assignee = (item.get("assignee") or "").strip()
    if assignee and name == assignee:
        raise WorkError("reviewer refused: %s holds lot #%s — a reviewer reviews "
                        "another agent's work" % (name, item.get("id")))
    return name


def _declared_harness(db: Db, name: str) -> str:
    """Le harnais **déclaré au registre** d'un agent (`""` si absent)."""
    from . import registry

    try:
        row = registry.get(db, name)
    except Exception:
        return ""
    return ((row or {}).get("harness") or "").strip().lower()


def reviewer_family_warning(db: Db, item: dict, reviewer: str) -> str:
    """#800 : avertit (sans refuser) un relecteur de la famille de l'auteur.

    Le canon §2 demande une relecture par une autre famille de modèle ; la
    désignation (#705) ne le vérifiait pas. « Même famille » se lit au harnais
    **déclaré au registre** (claude, codex et deepseek sont distincts :
    consigne mesh-design du 13/10) ; c'est un avertissement indicatif — la
    famille réelle se juge au tour de relecture (décision 18855a). Un humain,
    un agent hors registre ou un harnais inconnu n'avertissent pas, et la
    désignation est conservée dans tous les cas.
    """
    name = (reviewer or "").strip()
    author = (item.get("assignee") or "").strip()
    if not name or not author:
        return ""
    if name.startswith(HUMAN_PREFIX) or author.startswith(HUMAN_PREFIX):
        return ""
    author_harness = _declared_harness(db, author)
    if not author_harness or author_harness != _declared_harness(db, name):
        return ""
    return ("reviewer %s and the lot's author %s share the harness %r: canon §2 "
            "asks for a review by another model family (indicative warning, the "
            "designation is kept, #800)" % (name, author, author_harness))


def _review_item(cfg, db: Db, item_id: int, actor: str | None, *,
                 auditor: bool = False):
    """#705/#1979 : l'autorité du geste de relecture, et le lot, qui doit être
    en `qa` (comme pour `designate_reviewer`). `auditor=True` (#3324) ouvre
    la désignation et le retrait à l'auditeur ; `--no-verdict` garde le
    défaut, comme `--floor`."""
    who = corrector(cfg, db, actor, what="designation", auditor=auditor)
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if item["state"] == lifecycle.VALIDATED:
        raise WorkError(
            "lot %s is validated: move it to qa before changing reviewers "
            "(ameesh work move %s qa)" % (item_id, item_id))
    if item["state"] != lifecycle.QA:
        raise WorkError(
            "lot %s is %s, not qa: designate the reviewer on the entry to the "
            "review (ameesh work move %s qa --reviewer <agent>), or with "
            "ameesh work reviewer %s <agent> once it is in qa"
            % (item_id, item["state"], item_id, item_id))
    return who, item


def _write_reviewer(db: Db, item_id: int, value: str, who: str, event: str, *,
                    remove: bool = False) -> dict:
    """Écrit la colonne du relecteur (nom, vide, ou `REVIEWER_NONE`) et sa
    ligne de journal, ou dit pourquoi l'écriture n'a pas eu lieu."""
    row = storage.of(db).work.designate_reviewer(item_id, value, actor=who,
                                                event=event, remove=remove)
    if row is None:
        # #705 r2 (B1) : la règle est relue dans l'écriture ; entre la lecture
        # et elle, le lot a pu quitter qa ou recevoir `value` comme assigné.
        latest = get(db, item_id)
        if latest is not None and latest["state"] == lifecycle.QA:
            raise WorkError("reviewer refused: %s now holds lot #%s — a reviewer "
                            "reviews another agent's work" % (value, item_id))
        if latest is not None and latest["state"] == lifecycle.VALIDATED:
            raise WorkError(
                "lot %s became validated: move it to qa before changing reviewers "
                "(ameesh work move %s qa)" % (item_id, item_id))
        raise WorkError("lot %s left qa in the meantime: retry" % item_id)
    return row


def _reviewer_request(db: Db, item: dict, reviewer: str, who: str, *,
                      cfg=None) -> dict:
    """The request mail that wakes a manually designated reviewer (#2478).

    A designation writes the reviewer column and the journal; without a mail
    linked to the lot, the reviewer's runner had nothing to open a turn on and
    the review waited for a later `stale_lot` alert. The mail is a `request`
    (`kind`) carrying `work_item_id` (the `--lot` of `agent-mail`), so it is
    active mail for the runner's unread selection.

    The designation is already written when this runs: a delivery failure is
    returned as a warning (same pattern as `delegate`), never raised, so the
    caller can surface it without losing the designation. A human reviewer has
    no runner to wake and gets no mailbox request. An earlier request that
    already covers this reviewer and head is reused, not sent again (#2495).

    On a frozen head, the mail reads the reviewer's room as a corrector first
    (`review_correction.check`, the `--if-corrector` preview): too tight, it
    opens with "do not push" (#4325). An unreadable preview keeps the rule
    text, never delays the request.
    """
    from . import mail, plan_git
    from .review_designation import (REQUEST_KIND, covering_request,
                                     manual_reviewer_message)

    name = (reviewer or "").strip()
    if not name or name.startswith(HUMAN_PREFIX) or name == REVIEWER_NONE:
        return {}
    try:
        latest = plan_git.latest_freeze(milestones(db, int(item["id"]), limit=None))
    except Exception:  # noqa: BLE001 - unreadable milestones: no head named
        latest = None
    freeze = ({"id": int(latest["id"]), "sha": str(latest.get("sha") or "")}
              if latest is not None else None)
    # #2495/#2504 : one request per (lot, reviewer, head). A re-designation, or
    # a designation after the freeze already woke this reviewer, sends nothing.
    # The requests are selected per lot in storage: an old one cannot fall out
    # of a history window under a heavy mailbox.
    try:
        prior = covering_request(mail.review_requests(db, name, int(item["id"])),
                                 lot=int(item["id"]), reviewer=name,
                                 sha=freeze["sha"] if freeze else "")
    except Exception:  # noqa: BLE001 - unreadable requests: send, as before #2495
        prior = None
    if prior is not None:
        return {"reviewer_request_message_id": int(prior["id"]),
                "reviewer_request_reused": True}
    payload = {"review_request": {"lot": int(item["id"]), "reviewer": name,
                                  "manual": True}}
    if freeze:
        payload["review_request"]["freeze"] = freeze
    correction = None
    cfg = cfg if cfg is not None else getattr(db, "cfg", None)
    if freeze and cfg is not None:
        from . import review_correction

        try:
            correction = review_correction.check(cfg, db, int(item["id"]), name,
                                                 fetch=True)
        except Exception:  # noqa: BLE001 - un aperçu ne retient pas la demande
            correction = None
    body = manual_reviewer_message(item, freeze=freeze, actor=who,
                                   correction=correction)
    try:
        message_id = mail.send(
            db, who or SYSTEM_SENDER, name, body, kind=REQUEST_KIND,
            work_item_id=str(int(item["id"])), payload=payload)
    except Exception as exc:  # noqa: BLE001 - la désignation est écrite ; le dire
        return {"reviewer_request_warning":
                "review request not delivered to %s (%s): notify them" % (name, exc)}
    return {"reviewer_request_message_id": message_id}


def _settle_reviewer_requests(cfg, db, item: dict, reviewer: str,
                              who: str) -> list[int]:
    """#4779 : solde les demandes de relecture d'un siège qu'on retire.

    Le retrait d'un siège rend caduque la demande déposée chez le relecteur :
    sans ce règlement, elle reste en tête de sa boîte et le prochain tour du
    relecteur ouvre une relecture que plus personne n'attend (cas du canari
    `handoff-canary-3392` sur #4770). Le message est marqué remis, comme une
    demande à échéance (`mail.settle_review_requests`), et une trace part au
    fil du lot — sans destinataire réveillé. Rend les ids soldés.
    """
    from . import fil, mail

    name = str(reviewer or "").strip()
    if not name:
        return []
    try:
        ids = mail.settle_review_requests(db, name, int(item["id"]))
    except Exception as exc:  # noqa: BLE001 - le retrait doit dire l'échec
        raise WorkError("review request settlement failed for %s (lot #%s): %s"
                        % (name, item.get("id"), exc)) from exc
    if not ids:
        return []
    text = (
        "Demande(s) de relecture réglée(s) — siège retiré.\n\n"
        "Le siège de %s sur le lot #%s a été retiré (geste de %s). Les demandes "
        "encore non remises (%s) sont soldées sans tour, comme une demande à "
        "échéance (`request_expires_ts`) : aucun avis n'est attendu de ce siège."
        % (name, item.get("id"), who or SYSTEM_SENDER,
           ", ".join("#%d" % int(i) for i in ids)))
    if cfg is not None:
        # `fil.record` ne lève jamais : une trace manquante ne perd pas le
        # règlement, le message reste dans la boîte et son fil est réessayé
        # par l'index au prochain passage.
        fil.record(cfg, db, sender=SYSTEM_SENDER, recipients=[name], text=text,
                   project=str(item.get("app") or ""), lot=int(item["id"]),
                   meta={"review_request_settled": [int(i) for i in ids]})
    return ids


def designate_reviewer(cfg, db: Db, item_id: int, reviewer: str, *,
                       actor: str | None = None, remove: bool = False) -> dict:
    """#2076 : ajoute ou retire un relecteur d'un lot **déjà en `qa`**.

    Geste de pilote : mêmes autorités que la correction (`corrector`) — un
    humain, un orchestrateur ou un agent de conception — plus l'auditeur
    (#3324, fiche du 24/10, niveau 1). Le relecteur doit être
    joignable et différent de l'assigné (`check_reviewer_choice`). L'entrée en
    `qa` peut aussi le désigner d'un coup : `work move <id> qa --reviewer`.
    Même harnais déclaré que l'auteur : avertissement indicatif (#800), posé
    sur la ligne rendue (`reviewer_warning`), la désignation est conservée.
    La désignation dépose aussi un `request` lié au lot (#2478) : le relecteur
    est réveillé, son runner ouvre un tour ; une remise en échec est un
    avertissement (`reviewer_request_warning`), la désignation reste écrite.

    #4779 : un retrait règle les demandes de relecture encore non remises du
    siège (`reviewer_settled`, trace au fil du lot), même sur un lot sorti de
    `qa` ou dont le siège a déjà disparu : c'est le rejeu qui solde la demande
    caduque d'un canari. Hors `qa`, rien n'est écrit au siège et la ligne rend
    `reviewer_changed=False`.
    """
    if remove:
        # #4779 : le retrait règle d'abord les demandes de relecture encore
        # non remises chez l'agent, même quand le lot a quitté `qa` ou que le
        # siège n'y est déjà plus (le rejeu du cas réel #4770 doit solder le
        # message du canari). Aucune écriture de siège hors de `qa` : la ligne
        # rendue le dit (`reviewer_changed=False`).
        who = corrector(cfg, db, actor, what="designation", auditor=True)
        item = get(db, item_id)
        if item is None:
            raise WorkError("lot %s not found" % item_id)
        # A former reviewer can still be withdrawn after leaving the registry.
        name = (reviewer or "").strip()
        if not name:
            raise WorkError("empty reviewer")
        settled = _settle_reviewer_requests(cfg, db, item, name, who)
        if item["state"] != lifecycle.QA:
            row = dict(item)
            row["reviewer_changed"] = False
            row["reviewer_settled"] = settled
            return row
        row = _write_reviewer(db, item_id, name, who, "reviewer %s removed" % name,
                              remove=True)
        row["reviewer_settled"] = settled
        return row
    who, item = _review_item(cfg, db, item_id, actor, auditor=True)
    name = check_reviewer_choice(db, item, reviewer, cfg=cfg)
    if not name:
        raise WorkError("empty reviewer")
    warning = reviewer_family_warning(db, item, name)
    row = _write_reviewer(db, item_id, name, who, "reviewer %s designated" % name)
    if warning:
        row["reviewer_warning"] = warning
    row.update(_reviewer_request(db, item, name, who, cfg=cfg))
    return row


def clear_reviewer(cfg, db: Db, item_id: int, *, actor: str | None = None) -> dict:
    """#1979 : retire la désignation du relecteur d'un lot en `qa`.

    Geste de pilote (mêmes autorités que `designate_reviewer`, auditeur
    compris, #3324). Le lot
    retombe dans l'attente réelle d'un relecteur — « verdict d'un relecteur
    (non désigné) » — et l'alerte redemande d'en désigner un : c'est le geste
    inverse de `waive_review`, pour une désignation posée à tort."""
    who, item = _review_item(cfg, db, item_id, actor, auditor=True)
    # #4779 : chaque siège retiré solde sa demande encore non remise. On règle
    # avant l'écriture : si celle-ci échoue, un nouveau `--remove` retrouve les
    # noms dans la colonne et rejoue le règlement (idempotent).
    from .work_reviewers import designated

    settled: list = []
    for name in designated(item):
        settled.extend(_settle_reviewer_requests(cfg, db, item, name, who))
    row = _write_reviewer(db, item_id, "", who, "reviewer designation removed")
    if settled:
        row["reviewer_settled"] = settled
    return row


def waive_review(cfg, db: Db, item_id: int, *, actor: str | None = None) -> dict:
    """#1979 : marque « verdict non requis » sur un lot en `qa`.

    Geste de pilote : il constate qu'aucun re-verdict n'est dû — résolution de
    composition mécanique (canon §11 bis/#87), verdict conservé d'une tête
    relue. Le registre cesse alors d'attendre un verdict (l'attente devient la
    fusion) et les rappels ne partent plus à un relecteur. Désigner un
    relecteur, retirer la marque (`clear_reviewer`), sortir de qa ou geler
    une autre tête rend la revue à nouveau due."""
    who, item = _review_item(cfg, db, item_id, actor)
    if _blocked_current(db, item_id, item):
        raise WorkError(
            "verdict not required refused: lot %s carries a current blocked "
            "verdict — a fix is due, not a waiver (#1979)" % item_id)
    if _waived(item):
        # #4109 : la désignation d'un véhicule sans delta propre a déjà posé
        # la dispense ; le composeur horaire la redemande sans second journal.
        return item
    return _write_reviewer(db, item_id, REVIEWER_NONE, who,
                           "verdict not required: no re-verdict due (#1979)")


def _waived(item: dict) -> bool:
    names = item.get("reviewers")
    if names is None:
        names = [item.get("reviewer")]
    return REVIEWER_NONE in [str(name or "").strip() for name in names]


def _blocked_current(db: Db, item_id: int, item: dict) -> bool:
    """Un verdict bloquant COURANT demande une correction, pas une dispense :
    la marque dormirait et dispenserait la revue suivante à tort. Les
    transitions se lisent dans l'ordre chronologique (`lot_transitions`),
    pas le journal complet, qui rend le plus récent d'abord."""
    from . import progress  # import tardif : progress importe ce module
    store = storage.of(db)
    transitions = store.progress.lot_transitions([int(item_id)])
    # #2346 : `human:<nom>` et le nom nu ne sont le même acteur que si le nom
    # nu n'est pas un agent du registre.
    agents = {r["name"] for r in store.progress.agents(None) if r.get("name")}
    jalons = progress._jalons_de_lot(item, transitions, [], milestones(db, item_id),
                                     agents=agents)
    return bool(jalons.get("verdict_kind") == "blocked" and jalons.get("verdict_current"))


def waive_ownless_vehicle(db: Db, item_id: int, actor: str, event: str) -> bool:
    """#4109 : la dispense de `waive_review`, posée par la désignation.

    Un véhicule dont le delta propre est vide n'a aucun avis dû : ses membres
    sont relus sur leur tête gelée. Le geste est mécanique (§7), sans
    autorité de pilote ; il s'écrit dans la transaction de l'appelant. Rien
    n'est écrit hors `qa`, sur une dispense déjà posée ni sous un verdict
    bloquant courant. Rend True si la dispense a été écrite."""
    item = get(db, int(item_id))
    if item is None or item.get("state") != lifecycle.QA or _waived(item):
        return False
    if _blocked_current(db, int(item_id), item):
        return False
    row = storage.of(db).work.designate_reviewer(int(item_id), REVIEWER_NONE,
                                                actor=actor, event=event)
    return row is not None


def check_external_executor(db: Db, executor: str, *, cfg=None) -> str:
    """#3447 : l'exécutant externe déclaré d'un lot est-il joignable ?

    C'est un *déclaratif*, pas une attribution : le lot reste sans `assignee`
    (l'agent externe ne se réveille pas, 0030), mais il porte le nom de celui
    qui doit le faire — le bureau (`mode=externe` avec un responsable humain),
    ou un humain (`human:…`, nom nu connu normalisé comme pour l'assignation).

    Un agent `execute` est refusé : il s'attribue (`work assign`), il ne se
    réserve pas. Un agent externe sans responsable humain est refusé (0030,
    il ne porterait ni l'attente ni les alertes). Rend le nom normalisé.
    """
    from . import registry

    from .agent_names import resolve
    name = resolve((executor or "").strip(), db=db, operation="work.external-executor")
    if not name:
        raise WorkError("empty external executor")
    if name.startswith(HUMAN_PREFIX):
        return name
    cfg = cfg if cfg is not None else getattr(db, "cfg", None)
    row = registry.get(db, name)
    if row is None:
        human = known_human(db, name, cfg)
        if human:
            return human
        raise WorkError("external executor refused: %s is not in the registry; "
                        "an external executor is a registered `externe` agent with a "
                        "human owner, or a human (human:<id>)" % name)
    if (row.get("mode") or "execute") != "externe":
        raise WorkError("external executor refused: %s is a %s agent, not an external "
                        "one — assign it instead (`ameesh work assign <lot> %s`)"
                        % (name, row.get("mode") or "execute", name))
    if not (row.get("responsible") or "").strip():
        raise WorkError("external executor refused: external agent %s has no human "
                        "owner (0030); set one first (canon, or `ameesh set %s "
                        "responsible=human:<id>`)" % (name, name))
    return name


def check_assignee(db: Db, assignee: str, *, externe: bool = False, cfg=None) -> dict:
    """L37 (0030, règle 2) : un lot ne va qu'à un agent qu'ameesh sait réveiller.

    Rend `{assignee, externe, responsible, warning}` ou lève WorkError avec la
    raison. Un humain (`human:…`) est toujours accepté. Un agent doit être
    réveillable (`registry.wakeable` : connu, `execute`, admis, responsable
    s'il est requis). `externe=True` (option `--externe`) force l'attribution
    à un agent EXTERNE, seulement s'il a un humain responsable : c'est lui qui
    portera l'attente et les alertes du lot. Un nom inconnu est toujours
    refusé, un agent retiré aussi (#3460). Un agent arrêté reste accepté, avec
    un avertissement (`warning`).

    L46 : un nom NU inconnu du registre mais qui désigne un humain connu
    (`known_human` : Member d'un canon, humain déclaré) est normalisé en
    `human:<id>`, avec un avertissement — `assignee` rendu est alors le nom
    normalisé, à enregistrer. Un canon momentanément invalide ne bloque pas
    l'attribution (avertissement seulement). Les refus disent quoi faire.

    #1021 : un agent réveillable mais en tenue budget/rythme (`account_turn.
    turn_hold`, la garde de l'exécuteur) est refusé de même : son exécuteur ne
    lui donnerait pas de tour, le lot dormirait. C'est le point de passage
    unique des attributions (`work assign`, `work add --assignee`, courrier
    `--lot`/`--new-lot`, `delegate`, `backlog.auto_take`, `handoff`).
    #4056 : la seule fenêtre courte du compte (#3779) n'est qu'un
    avertissement : elle se lève en minutes.
    """
    from . import registry

    from .agent_names import resolve
    name = resolve((assignee or "").strip(), db=db, operation="work.assignee")
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
    if row.get("retired_ts"):
        # #3460 : un retiré n'est jamais réclamé, le lot dormirait
        raise WorkError("assignment refused: %s is retired (ameesh agent retire, "
                        "#3460): nobody would work on this lot; %s"
                        % (row.get("name") or name, _ASSIGNEE_HINT))
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
        elif row.get("operator_paused"):
            # #2200 : ce n'est pas une inscription à réparer — la pause
            # humaine se lève nommément, ou le lot va à un autre agent.
            hint = (" — lift the operator pause (ameesh resume %s --operator "
                    "--reason <reason>), or assign another agent (the team queue "
                    "takes an unassigned lot)" % name)
        else:
            hint = (" — fix its registration or admission (agent-runner register "
                    "…, canon sync), or assign to a human (human:<id>)")
        raise WorkError("assignment refused: %s%s" % (why, hint))
    # #1021 : réveillable ne suffit pas — un agent en tenue budget/rythme ou
    # en échec d'authentification ne sera pas réveillé par son exécuteur et le
    # lot dormirait (#885/#886/#967, confiés à des agents Codex en pause
    # budget). Un agent arrêté garde son avertissement : l'arrêt est un geste
    # humain, réversible, pas une tenue.
    warnings = []
    if row.get("status") not in ("stopped", "dead"):
        from . import account_turn

        hold = account_turn.turn_hold(cfg, db, name, row)
        if hold and _short_window_hold(hold):
            # #4056 : la fenêtre courte du compte (#3779 : écart de départ,
            # cooldown, plafond de tours) se lève en minutes, et sur un compte
            # partagé l'écart de départ se renouvelle à chaque tour d'un voisin
            # (cx1/cx2 le 27/10, 19:33) : le refus ne passait jamais. Le lot
            # attend le prochain tour admis, comme une désignation (#3876).
            warnings.append("%s is briefly held — %s; the lot waits for its next "
                            "admitted turn" % (name, hold))
        elif hold:
            raise WorkError(
                "assignment refused: %s is held — %s; its runner will not start a "
                "turn before the hold lifts: wait for the release, assign another "
                "agent, or leave the lot unassigned (the team queue takes it)"
                % (name, hold))
    if row.get("status") == "stopped":
        warnings.append("%s is stopped (%s): the lot will wait for it to resume"
                        % (name, row.get("stop_reason") or "unknown reason"))
    if note:
        warnings.append(note)
    return {"assignee": name, "externe": False, "responsible": responsible,
            "warning": "; ".join(warnings) or None}


def _short_window_hold(hold: str) -> bool:
    """#4056 : la tenue rendue par `account_turn.turn_hold` (préfixe de pause
    déjà retiré, marque d'agent critique possible) est-elle la seule fenêtre
    courte du compte (#3779), qui dure au plus `COOLDOWN_MAX_S` ?"""
    from . import account_window, budget

    text = (hold or "").strip()
    if text.lower().startswith(budget.CRITICAL_MARK.lower()):
        text = text[len(budget.CRITICAL_MARK):]
    return account_window.is_short_window(text)


def is_pilot(cfg, db: Db, actor: str) -> bool:
    """L'acteur a-t-il l'autorité du pilote ? — un humain, ou un orchestrateur
    ou agent de conception (`assignments.is_orchestrator` avec
    `CORRECTOR_ROLES`, la même autorité que `work move --correct`). Sert à
    l'exception EXPLICITE à la limite d'en-cours (§4) : jamais une exemption
    silencieuse."""
    from . import assignments

    name = (actor or "").strip()
    if not name:
        return False
    if name.startswith(HUMAN_PREFIX):
        return True
    try:
        return assignments.is_orchestrator(
            cfg if cfg is not None else getattr(db, "cfg", None), db, name,
            roles=CORRECTOR_ROLES)
    except Exception:  # noqa: BLE001 - autorité illisible : pas d'exception
        return False


#: #5006 : marque machine d'un geste de statut (move, gel) posé par un agent
#: qui n'est plus l'assigné du lot — dans la note du journal et sur stderr.
NON_ASSIGNEE_MARK = "ameesh-non-assignee:"


def non_assignee_warning(cfg, db: Db, item: dict, actor: str) -> str:
    """#5006 : l'avertissement d'un geste de statut (`work move`, gel) posé
    par un agent qui n'est pas l'assigné du lot, ou ''.

    Signalé, jamais refusé : un humain, le système, un pilote (`is_pilot`) ou
    un relecteur désigné du lot agissent légitimement sur le lot d'un autre.
    Le cas visé est l'ancien assigné dont le tour a continué après une
    réassignation (#4858 : intake → build et gel après `work assign`)."""
    name = (actor or "").strip()
    holder = (item.get("assignee") or "").strip()
    if not name or not holder or name == holder:
        return ""
    if name.startswith(HUMAN_PREFIX) or name in (SYSTEM_SENDER, UNKNOWN_ACTOR):
        return ""
    from .work_reviewers import designated
    if holder.startswith(HUMAN_PREFIX) or name in designated(item):
        return ""
    if is_pilot(cfg, db, name):
        return ""
    return ("%s %s is not the assignee of lot #%s (assignee: %s); if the lot was "
            "reassigned away from you, stop: no push, freeze or move — leave a "
            "note of your state for %s" % (NON_ASSIGNEE_MARK, name, item.get("id"),
                                           holder, holder))


def _notify_reassigned(db: Db, item: dict, previous: str | None, assignee: str,
                       actor: str) -> int | None:
    """#5006 : prévient l'ancien assigné d'une réassignation.

    Le courrier est passif (`info` : il n'ouvre pas de tour à un agent au
    repos, son prochain brief relit l'assigné courant) et `reassigned`, une
    clé remise EN COURS de tour (`mail.MID_TURN_KEYS`, L514) : un tour déjà
    lancé sur le lot l'apprend au prochain appel d'outil. Rien pour un
    humain, un externe, ni quand l'ancien assigné réassigne lui-même. Un
    échec d'envoi ne retient pas la réassignation déjà écrite."""
    from . import fil, mail
    old = (previous or "").strip()
    if not old or old == assignee or old == (actor or "").strip() \
            or old.startswith(HUMAN_PREFIX):
        return None
    lot = int(item["id"])
    to = assignee or "the team queue (nobody)"
    body = ("Lot #%d (%s) is reassigned to %s by %s: it is no longer yours. "
            "If a turn of yours is working on it, stop now: no push, no freeze, "
            "no move. Leave a note of your state (`ameesh work note %d …`: branch, "
            "pushed head, what remains) so that %s can take it over."
            % (lot, fil.excerpt(item.get("title") or "", 120), to,
               actor or "nobody", lot, assignee or "the next assignee"))
    try:
        return mail.send(db, SYSTEM_SENDER, old, body, work_item_id=str(lot),
                         payload={"info": True,
                                  "reassigned": {"lot": lot, "to": assignee,
                                                 "by": actor or ""}})
    except Exception:  # noqa: BLE001 - l'attribution est écrite ; le journal la dit
        return None


def assign(db: Db, item_id: int, assignee: str, *, externe: bool = False, actor: str = "",
           cfg=None, branch: str | None = None, branch_target: str | None = None,
           force: str | None = None, claim: bool = False,
           if_holder: str | None = None,
           wip_exception: str | None = None) -> dict:
    """Réassigne un lot ouvert (L37, `ameesh work assign`), sous la même garde
    que la création (`check_assignee`). Une ligne de journal dit l'ancien et
    le nouvel assigné. Rend `{item, check, previous}`.

    L118 : `branch` (et `branch_target`) posent aussi la branche du lot ; avec
    une branche, un lot déjà assigné à cet agent n'est pas une erreur : seule
    la branche est posée, sans attribution — la délégation en cours (L40) et
    l'exception pilote (§4) restent en place. #705 : si le nouvel assigné est le
    relecteur désigné du lot, la désignation tombe (le journal le dit) — un
    relecteur ne relit pas son propre travail.

    #1026 : `claim` n'attribue le lot que s'il est **sans assigné**. Un lot
    déjà pris est refusé en nommant son porteur, et l'écriture est
    conditionnelle à `assignee` encore vide : deux prises concurrentes, une
    seule gagne, l'autre refuse sans changer la main.

    #1787 : une re-prise par le porteur lui-même (`claim` sur son propre lot)
    est un no-op réussi (`noop`) : l'attribution ne change pas et n'est pas
    journalisée ; seule une `branch` fournie est enregistrée (`set_branch`).

    #2016 : `if_holder` nomme le titulaire lu par l'appelant (l'instantané
    d'une alerte) ; si le lot a changé de main depuis, la réassignation est
    refusée en nommant le porteur courant. Deux agents partis du même brief ne
    se volent plus le lot : le second relit le lot au lieu d'écraser le
    premier.

    §4 : `wip_exception` (une raison non vide) pose une exception pilote
    EXPLICITE à la limite d'en-cours — réservée à un humain ou à un pilote
    (`is_pilot`), elle est écrite dans la note de la dernière attribution (même
    assigné : c'est le geste qui la réécrit), et le balayage (`ameesh work
    wip`) laisse alors le lot au-delà du plafond. Elle s'efface à l'attribution
    suivante sans marqueur.

    #3447 : un lot réservé à un exécutant externe (`external_executor`) ne
    s'assigne pas à un agent — deux exécutants, et L165 ne le prend déjà pas.
    Le pilote lève la réservation (`ameesh work external-executor <lot>
    --clear`) ou y fait travailler l'agent sur un sous-lot de préparation."""
    from .agent_names import resolve as resolve_agent_name
    assignee = resolve_agent_name(assignee, db=db, operation="work.assign")
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if item["state"] in MERGED_STATES or item["state"] == lifecycle.CLOSED:
        raise WorkError("lot %s already %s: nothing to reassign" % (item_id, item["state"]))
    previous = item.get("assignee")
    reserved = (item.get("external_executor") or "").strip()
    if reserved:
        raise WorkError(
            "lot %s is reserved to %s (external executor, #3447): it is not assignable — "
            "clear the reservation first (`ameesh work external-executor %s --clear`) or "
            "give the agent a preparation sub-lot" % (item_id, reserved, item_id))
    if if_holder is not None:
        expected = str(if_holder).strip() or None
        if (previous or None) != expected:
            raise WorkError(
                "lot %s changed holder since the reading: now %s (expected %s); "
                "re-read the lot and retry" % (item_id, previous or "nobody",
                                               expected or "nobody"))
    _branch_args(branch, branch_target)
    reason = " ".join(str(wip_exception).split()) if wip_exception is not None else ""
    if wip_exception is not None and not reason:
        raise WorkError("--wip-exception: a reason is required (the exception is "
                        "explicit, never implicit)")
    if reason and not is_pilot(cfg, db, actor):
        raise WorkError("WIP exception refused: %s is neither a human nor an "
                        "orchestrator/design agent; the exception to the §4 limit is "
                        "explicit and reserved to the pilot" % (actor or "nobody"))
    check = check_assignee(db, assignee, externe=externe, cfg=cfg)
    if claim and previous:
        if previous == check["assignee"]:
            if not reason:
                if branch:
                    row = set_branch(db, item_id, branch, branch_target, actor=actor)
                    return {"item": row, "check": check, "previous": previous, "noop": True}
                return {"item": item, "check": check, "previous": previous, "noop": True}
            # la re-prise par le porteur avec une exception §4 explicite se
            # journalise plus bas : l'attribution ne change pas.
        else:
            raise WorkError("lot %s already assigned to %s: claim refused, nothing changed"
                            % (item_id, previous))
    if previous == check["assignee"] and not reason:
        if not branch:
            raise WorkError("lot %s already assigned to %s" % (item_id, previous))
        # L118 : le lot est déjà à cet agent — seule la branche est posée,
        # sans attribution (ni note, ni annulation de délégation) : la
        # délégation en cours (L40) et l'exception pilote (§4), portées par la
        # dernière attribution, restent en place.
        row = set_branch(db, item_id, branch, branch_target, actor=actor)
        return {"item": row, "check": check, "previous": previous}
    if not previous and takes_rule(check):
        # #2584 (canon §2 l.129) : un élément du backlog sous le seuil de lot
        # du canon ne se confie pas à un agent tant qu'un lot de roadmap de
        # son projet est prenable — même règle que la prise au repos.
        from . import backlog
        refusal = backlog.assign_refusal(
            db, item, cfg=cfg if cfg is not None else getattr(db, "cfg", None))
        if refusal:
            raise WorkError(refusal)
    note = "assigned to %s%s (before: %s)%s" % (
        check["assignee"],
        " — external session, owner %s" % check["responsible"] if check["externe"] else "",
        previous or "nobody",
        # L40 (0030) : la réassignation efface la délégation en cours
        "; delegation from %s cancelled" % item["delegated_by"]
        if item.get("delegated_by") and item.get("due_ts") else "")
    if reason:
        note += "; %s (pilot, %s): %s" % (WIP_EXCEPTION_MARK, actor or UNKNOWN_ACTOR, reason)
    # #4667 : un véhicule de paquet (`integration/**`) n'entre pas dans
    # l'en-cours d'un agent — le geste explicite qui l'attribue ne le met pas
    # non plus dans la file d'équipe (#540).
    # #4756 : le lot de découpage non plus — c'est un geste du pilote.
    from .file_decoupage import is_split_lot
    queued = (None if _is_vehicle_branch(branch or item.get("branch")) or is_split_lot(item)
              else _queued_note(check, "not assigned to %s (before: %s, now: nobody)"
                                % (check["assignee"], previous or "nobody")))
    # L165 (EP4) : l'attribution est un geste du pilote hors base (elle
    # réveille un autre agent). Le bail élu est vérifié et journalisé dans la
    # même transaction que l'écriture ; un acteur humain ou un exécuteur sans
    # session liée n'est pas concerné (`gate` rend None).
    from . import pilot_gesture
    fence = pilot_gesture.gate(cfg, db, "assign", "#%d" % item_id,
                               project=(item.get("app") or None))
    try:
        if fence is None:
            row = storage.of(db).work.assign(
                item_id, check["assignee"],
                current=(None if claim and not previous else previous),
                note=note, actor=actor, queued_note=queued,
                forced_note=queued and _forced_note(check, note, force, actor))
        else:
            with db.transaction() as tx:
                fence.hold(tx)
                row = storage.of(tx).work.assign(
                    item_id, check["assignee"],
                    current=(None if claim and not previous else previous),
                    note=note, actor=actor, queued_note=queued,
                    forced_note=queued and _forced_note(check, note, force, actor))
    except storage.FootprintOverlap as exc:
        # #4173 : l'empreinte du lot chevauche celle d'un lot en cours du
        # même dépôt — rien n'est écrit, le lot attend sa fusion.
        from . import work_footprints
        raise WorkError(work_footprints.refusal(item_id, exc.overlaps)) from None
    if row is None:
        if claim:
            holder = (get(db, item_id) or {}).get("assignee")
            raise WorkError("lot %s taken in the meantime%s: claim refused, nothing changed"
                            % (item_id, " by %s" % holder if holder else ""))
        raise WorkError("lot %s changed in the meantime: retry" % item_id)
    behind = row.get("queued_behind")
    if branch:
        row = set_branch(db, item_id, branch, branch_target, actor=actor)
    out = {"item": row, "check": check, "previous": previous}
    if (row.get("assignee") or None) != (previous or None):
        # #540 : un lot rendu à la file d'équipe quitte aussi l'ancien assigné
        notified = _notify_reassigned(db, item, previous,
                                      row.get("assignee") or "", actor)
        if notified is not None:
            out["previous_notified"] = notified
    if behind is not None:
        row["queued_behind"] = behind
        out["queue_warning"] = queue_warning(db, row, check["assignee"])
    return out


def set_external_executor(db: Db, item_id: int, executor: str | None, *,
                          actor: str = "", cfg=None, clear: bool = False) -> dict:
    """#3447 : déclare (ou efface, `clear=True`) l'exécutant externe d'un lot.

    Geste de pilote (`corrector`, `history=False`), comme la déclaration à la
    création. Le lot reste sans assigné : l'exécutant externe ne se réveille
    pas (0030), il porte l'attente nommée par les alertes et le débit. Un lot
    déjà assigné est refusé (deux exécutants) ; le lot doit être ouvert. Rend
    `{item, previous, executor}`."""
    who = corrector(cfg, db, actor, what="external executor", history=False)
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if item["state"] in MERGED_STATES or item["state"] == lifecycle.CLOSED:
        raise WorkError("lot %s is %s: no external executor to declare"
                        % (item_id, item["state"]))
    previous = (item.get("external_executor") or "").strip() or None
    if clear:
        name = None
        note = ("%s (was %s)" % (interface.EXTERNAL_EXECUTOR_CLEARED_NOTE, previous)
                if previous else interface.EXTERNAL_EXECUTOR_CLEARED_NOTE)
    else:
        name = check_external_executor(db, executor or "", cfg=cfg)
        if item.get("assignee"):
            raise WorkError("lot %s is assigned to %s: clear the assignment first "
                            "(one executor per lot)" % (item_id, item["assignee"]))
        if name == previous:
            raise WorkError("lot %s is already reserved to %s" % (item_id, name))
        note = "%s %s" % (interface.EXTERNAL_EXECUTOR_NOTE, name)
    row = storage.of(db).work.set_external_executor(item_id, name, note=note, actor=who)
    if row is None:
        current = get(db, item_id)
        holder = (current or {}).get("assignee")
        if holder:
            raise WorkError("lot %s was assigned to %s in the meantime: one executor "
                            "per lot, nothing changed" % (item_id, holder))
        raise WorkError("lot %s changed or closed in the meantime: retry" % item_id)
    return {"item": row, "previous": previous, "executor": name}


def takes_rule(check: dict) -> bool:
    """#540 : l'assigné normalisé par `check_assignee` est soumis à la règle
    « au plus un lot non démarré » (ni humain ni agent externe)."""
    return not (check["externe"] or check["assignee"].startswith(HUMAN_PREFIX))


def _queued_note(check: dict, head: str) -> str | None:
    """#540 : la note du journal d'un lot mis dans la file d'équipe faute de
    place chez l'agent (`{lot}` : son lot déjà en attente) ; None pour un
    humain ou un agent externe, hors de la règle."""
    if not takes_rule(check):
        return None
    return "%s; team queue (#540): %s already has lot #{lot} not started" % (
        _braces(head), check["assignee"])


def _forced_note(check: dict, head: str, reason: str | None, actor: str) -> str | None:
    """#540 (canon §4, « sauf --force motivé ») : la note d'une attribution
    forcée malgré un lot non démarré ; None sans raison (pas de forçage)."""
    reason = " ".join(str(reason or "").split())
    if not reason or _queued_note(check, head) is None:
        return None
    return ("%s; team queue (#540) forced by %s: %s already has lot #{lot} not started; "
            "reason: %s" % (_braces(head), _braces(actor or "?"), check["assignee"],
                            _braces(reason)))


def _braces(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")


def lot_team(db: Db, item: dict) -> str | None:
    """L'équipe du lot comme la lit la prise L165 : la sienne, sinon celle de
    sa fiche ou de la fiche parente, sinon son application."""
    st = storage.of(db)
    for value in (item.get("team"),
                  *((st.packages.get(p) or {}).get("team")
                    for p in (item.get("package_id"), item.get("package_parent")) if p),
                  item.get("app")):
        if (value or "").strip():
            return value.strip()
    return None


def queue_warning(db: Db, item: dict, agent: str) -> str:
    """#540 : ce qu'on dit à qui attribue un lot mis dans la file d'équipe."""
    team = lot_team(db, item)
    later = "ameesh work assign once %s starts #%s" % (agent, item["queued_behind"])
    return ("lot #%s put in the team queue: %s already has lot #%s not started (at most one "
            "per agent, #540); %s" % (
                item["id"], agent, item["queued_behind"],
                "it is %s: it waits for the orchestrator (%s)" % (item["state"], later)
                if item.get("state") != lifecycle.REQUESTED else
                "an idle agent of team %s will take it (L165)" % team if team else
                "it has no team: it waits for the orchestrator (%s)" % later))


def build_limit(cfg) -> int:
    """§4 canon : le plafond de lots en développement (`build`) par agent.

    Lu au canon compilé (`build.max_in_build`, #2451), jamais une constante
    locale ; les lots en surplus retournent à la file d'équipe. Règle
    générique du document global, pour tous les projets et tous les agents.
    Sans document lisible, `mechanics.MechanicsError` : l'appelant refuse
    d'agir plutôt que de supposer un plafond.
    """
    from . import mechanics
    return mechanics.configured(cfg).max_in_build


def return_to_queue(db: Db, item_id: int, *, actor: str = "", cfg=None,
                    reason: str = "") -> dict:
    """§4 : rend un lot à la file d'équipe — l'assignation est retirée (CAS sur
    l'assigné lu ET l'état, journal dans la même transaction) ; branche, tête de
    branche, gels et avis (jalons) restent. Le lot garde son état, donc un agent
    libre le reprend avec sa branche (`unassigned_lots`).

    Seuls un lot `intake` ou `build` se rendent (`RETURN_STATES`) : un lot en
    revue (`qa`), en attente d'un humain ou bloqué n'est pas de l'en-cours d'un
    agent.

    Autorité : l'assigné lui-même, un humain, ou un pilote (`is_pilot`) — un
    agent ne rend pas le lot d'un autre."""
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if item["state"] not in RETURN_STATES:
        raise WorkError("lot %s is %s: only an intake or build lot returns to the team "
                        "queue (§4)" % (item_id, item["state"]))
    current = (item.get("assignee") or "").strip()
    if not current:
        raise WorkError("lot %s has no assignee: already in the team queue" % item_id)
    who = (actor or "").strip()
    if who != current and not who.startswith(HUMAN_PREFIX) and not is_pilot(cfg, db, who):
        raise WorkError("return refused: %s is neither the assignee (%s), a human, nor "
                        "an orchestrator/design agent" % (who or "nobody", current))
    note = "%s by %s (§4: lots in build per agent, capped by the canon)" % (
        RETURN_NOTE, who or UNKNOWN_ACTOR)
    if reason.strip():
        note += ": %s" % " ".join(str(reason).split())
    row = storage.of(db).work.unassign(item_id, current=current, note=note,
                                       actor=who or UNKNOWN_ACTOR,
                                       states=RETURN_STATES)
    if row is None:
        raise WorkError("lot %s changed in the meantime: retry" % item_id)
    return row


def count_builds(db: Db, assignee: str) -> int:
    """§4 : les lots en `build` de l'agent (tous projets)."""
    return storage.of(db).work.count_builds(assignee)


# --------------------------------------------------------------------------
# délégation à échéance (L40, point 5 de la décision 0030)
# --------------------------------------------------------------------------

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

    #3447 : un lot réservé à un exécutant externe (`external_executor`) ne se
    délègue pas : la délégation écrit un `assignee` (deux exécutants) et
    réveillerait un agent. Le pilote lève la réservation d'abord.

    Le délégant est `actor`, à défaut l'assigné actuel (un `human:…` est
    admis) ; c'est à lui que le lot reviendra. Le délégué passe par
    l'attribution gardée (`check_assignee`, mêmes refus que `work assign`) et
    doit être un AGENT : une échéance se mesure en tours. Une nouvelle
    délégation remplace la précédente (et son échéance). Un événement
    (`kind=event`, lié au lot) réveille le délégué. #705 : déléguer le lot à
    son relecteur désigné retire la désignation (le journal le dit).

    Rend `{item, delegation, replaced, check, previous, message_id, warning}`.
    """
    from .agent_names import resolve as resolve_agent_name
    agent = resolve_agent_name(agent, db=db, operation="work.delegate")
    within_s = parse_within(within)
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if item["state"] in MERGED_STATES or item["state"] == lifecycle.CLOSED:
        raise WorkError("lot %s already %s: nothing to delegate" % (item_id, item["state"]))
    # #3447 : un lot réservé à un exécutant externe n'est pas délégable —
    # déléguer écrit un `assignee`, donc deux exécutants, et réveillerait un
    # agent que le pilote n'a pas désigné.
    reserved = (item.get("external_executor") or "").strip()
    if reserved:
        raise WorkError(
            "delegation refused: lot %s is reserved to %s (external executor, #3447): "
            "it is not assignable — clear the reservation first "
            "(`ameesh work external-executor %s --clear`)" % (item_id, reserved, item_id))
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
        # #3447 : la course réservation/délégation est tranchée sous le verrou
        # du storage ; la perdante dit ce qui a changé, pas « retry ».
        latest = get(db, item_id)
        reserved_now = ((latest or {}).get("external_executor") or "").strip()
        if reserved_now:
            raise WorkError(
                "delegation refused: lot %s was reserved to %s in the meantime "
                "(external executor, #3447): one executor per lot — the reservation "
                "wins, nothing was delegated" % (item_id, reserved_now))
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
            if row["state"] in MERGED_STATES or row["state"] == lifecycle.CLOSED \
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
    from .agent_names import resolve as resolve_agent_name
    agent = resolve_agent_name(agent, db=db, operation="work.mark_delegate_turn")
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
            or item.get("state") == lifecycle.CLOSED:
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


def _notify_released(db: Db, item_id: int, *, blocker_state: str,
                     blocker_reason: str = "", after_commit: list | None = None) -> list[dict]:
    """L385 suite : la fermeture d'un lot réveille les agents des lots qui le
    déclarent en prérequis explicite.

    Appelé DANS la transaction de fermeture, après l'écriture de la transition
    et sous le verrou de graphe : le courrier est committé avec la fermeture.
    Il n'y a donc pas de fenêtre « fermeture commise, réveil non écrit » ; un
    échec d'écriture annule la fermeture entière et la reprise refait les deux
    (voir `work_dependencies.notify_released`). Le fil lisible est écrit après
    le commit, par la liste `after_commit` que l'appelant exécute."""
    from . import work_dependencies
    return work_dependencies.notify_released(
        db, int(item_id), blocker_state=blocker_state,
        blocker_reason=blocker_reason, after_commit=after_commit)


def _pr_number(pr_ref) -> "int | None":
    """Le numéro de PR d'une référence `owner/repo#N`, ou None."""
    text = str(pr_ref or "")
    head, _, tail = text.rpartition("#")
    return int(tail) if head and tail.isdigit() else None


def _review_after_merge(db, item_id, *, sha: str = "", pr_ref=None, repo=None) -> dict:
    """L539 : toute fusion **enregistrée** ouvre le lot de relecture du canon §2.

    La file de fusion l'ouvre à l'atterrissage (`land`) ; ici passent les
    autres voies — relevé des branches (L118), `sync-merges`, `sync-github`,
    porte `git-merge`, `ameesh work merged` — et l'ouverture y est idempotente
    (`review_lot.open_for_merge`). `repo` porte le dépôt où la fusion a été
    constatée quand l'appelant le connaît (indice de classe).

    Rend le constat de `review_lot.open_for_merge` (`id`, `created`, `why`),
    jamais une exception : la fusion est un fait **déjà écrit**, rien ici ne
    doit remonter jusqu'à l'appelant et transformer un fait acquis en échec.
    Un `id` nul signale une relecture encore manquante — le diagnostic que
    `catch_up` reprend."""
    try:
        from . import review_lot
        return review_lot.open_for_merge(db, int(item_id), merged_sha=sha or "",
                                         pr=_pr_number(pr_ref), repo=repo) or {}
    except Exception as exc:  # noqa: BLE001 - la fusion reste un fait
        return {"id": None, "created": False,
                "why": "lot de relecture non ouvert : %s: %s" % (
                    type(exc).__name__, str(exc)[:160])}


def _resume_review_after_merge(db, item_id, *, sha: str = "", pr_ref=None,
                               repo=None, explicit: bool = False) -> dict:
    """L539 (#1104) : reprendre la relecture manquante d'un lot **déjà** fusionné.

    B1 de la R1 : ce chemin est rappelé par `sync-github` pour des lots
    fusionnés avant l'activation (`review_catch_up_bound`, migration 0438) —
    sans borne, chacun recevrait sa relecture avec l'échéance du jour, le
    rattrapage historique que l'arbitrage pilote du 15/10 exclut. Le prédicat
    partagé (`review_lot.catch_up_eligible`) dit la même vérité que `catch_up`
    (borne, relecture manquante, marque terminale, tentatives) ; hors bornes,
    rien n'est tenté et le lot reste au seul rattrapage borné.

    `explicit` (#3413) : le geste humain `ameesh work merged <id> --repo …`
    fournit le clone qui manquait au balayage ; il relance la classification
    sans attendre le délai de reprise, mais reste tenu par la borne
    d'activation (aucun historique relu)."""
    try:
        from . import review_lot
        existing = review_lot.opened_for(db, int(item_id))
        if existing is not None:
            return {"id": int(existing["id"]), "created": False,
                    "why": "lot de relecture #%d déjà ouvert (état %s)"
                           % (int(existing["id"]), existing.get("state") or "?")}
        if not review_lot.catch_up_eligible(db, int(item_id), explicit=explicit):
            why = ("hors bornes du rattrapage (fusion antérieure à l'activation"
                   " ou borne illisible) : aucune relecture due" if explicit else
                   "hors bornes du rattrapage (fusion antérieure à l'activation"
                   " ou marque terminale) : laissé à review_lot.catch_up")
            return {"id": None, "created": False, "why": why}
        return _review_after_merge(db, item_id, sha=sha, pr_ref=pr_ref, repo=repo)
    except Exception as exc:  # noqa: BLE001 - la fusion reste un fait
        return {"id": None, "created": False,
                "why": "reprise de relecture impossible : %s: %s" % (
                    type(exc).__name__, str(exc)[:160])}


def close_merged(db: Db, item_id: int, *, sha: str = "", actor: str = "", source: str = "",
                 pr_ref: str | None = None, frozen_id: int | None = None,
                 declared: bool = False, extra: str = "", repo: str | None = None,
                 landed: tuple[int, str] | None = None, unfrozen: bool = False,
                 content_guard=None, retry_review: bool = False,
                 connector: str = "") -> dict:
    """Fusion constatée : le lot passe `merged` et son jalon `merged` est posé.

    `connector` (L99 T2, #4350) : le connecteur de `deliver` qui porte la
    fusion, noté au journal ; vide pour un constat des balayages.

    Idempotent : un lot déjà fusionné reste tel quel (`already`). Jamais de
    réouverture silencieuse : un lot fermé (abandonné, remplacé) n'est pas
    touché, le constat est rendu (`refused`) pour être dit à l'appelant. Rend
    `{id, result: merged|already|refused|refrozen|withheld, detail, item}`.

    `frozen_id` (preuve par le contenu) : le jalon `frozen` examiné ; si ce
    n'est plus le dernier gel du lot au moment d'écrire, rien n'est fermé
    (`refrozen`) — le nouveau gel n'a pas été examiné. `unfrozen` (#2444,
    gel d'origine verdict) : aucun gel n'a été examiné ; un gel déclaré
    depuis rend aussi `refrozen`.

    `landed` (#1567) : `(numéro, dépôt)` du numéro réellement atterri, écrit
    dans la transaction de la fusion ; l'appelant l'a validé et réservé.

    #3022 (canon §5) : sous le même verrou, un lot marqué `prod-gate:` n'est
    fermé que si son correctif est sur la branche de prod de son projet
    (`prod_gate.check`) ; sinon `withheld`, motif « en attente prod ».

    `content_guard` (#2744) : callable `(storage, row) -> retention | None`,
    appelé **sous le verrou de ligne du lot**, juste avant la fermeture ; une
    retenue rend `withheld` sans rien écrire. Une retenue `lift: True` (#3219,
    la règle 1 de l'assigné) ne retient pas : sa levée est écrite dans la note
    de fusion. Le verrou est aussi pris par les notes `hold:`/`unhold:`
    (`work.note` le prend avant `note`) et par les transitions d'état : une
    retenue ne peut plus se glisser entre la lecture du journal et la
    fermeture.

    La note du journal et du jalon dit comment la fusion est connue :
    « fusion constatée par <source> », ou « fusion déclarée par … »
    (`declared`, `ameesh work merged`), suivie de `extra`.

    Dans la MÊME transaction que la transition, les agents des lots qui
    déclarent ce lot en prérequis explicite et dont c'était la dernière attente
    reçoivent un courrier durable (L385 suite) :
    le réveil ne peut être ni perdu (un échec annule la fusion, la reprise
    refait les deux) ni doublé (le verrou de graphe sérialise les fermetures,
    une seule voit la dernière attente tomber).

    L539 (#1104) : une fusion enregistrée, par quelque voie que ce soit, ouvre
    aussi le lot de relecture du canon §2 (`_review_after_merge`), y compris
    hors file de fusion. `repo` est le dépôt où la fusion a été constatée
    (le relevé le connaît) ; sans lui, la classe se lit sur le dossier de
    travail de l'assigné, puis sur le dépôt du projet, et un doute n'ouvre
    rien. Le lot de la file l'ouvre à l'atterrissage : l'ouverture est
    idempotente. Un lot déjà fusionné (`already`) reprend lui aussi l'ouverture
    manquante — la fusion reste acquise, et seulement dans les bornes du
    rattrapage (`review_lot.catch_up_eligible`, B1 de la R1 #1104) ;
    `review_lot.catch_up` reprend les lots fusionnés que les relevés ne
    sélectionnent plus.

    `retry_review` (#3413) : le chemin `already` est un geste **explicite**
    qui fournit le clone (`ameesh work merged <id> --repo …`) ; il relance la
    classification sans subir le délai ni le plafond de tentatives du
    balayage (la borne d'activation reste). Sans lui, l'avertissement
    « aucun dépôt lisible » conseillait un geste qui répondait « nothing to
    do ».
    """
    for _attempt in range(3):
        item = get(db, item_id)
        if item is None:
            raise WorkError("lot %s not found" % item_id)
        state = item["state"]
        pinned = _pinned_ref(item)
        if pinned:
            # #1922 : sous workflow, l'état `merged` est écrit par le registre
            # (garde serveur `merged_and_staging_deployed`), jamais par le
            # constat de fusion seul.
            return {"id": int(item_id), "result": "refused", "item": item,
                    "detail": "lot pinned to workflow %s: transitions go through "
                              "process_registry" % pinned}
        if state in MERGED_STATES:
            # L539 (#1104) : la fusion est acquise, mais sa relecture peut
            # manquer (l'ouverture avait échoué). Ce chemin idempotent la
            # reprend sans jamais rouvrir ni défaire la fusion (R1 #1104 P2-2),
            # et seulement dans les bornes du rattrapage (B1 de la R1) : un lot
            # fusionné avant l'activation n'est pas relu rétroactivement. Un
            # geste explicite avec le clone (#3413) relance la classification
            # sans attendre le délai de reprise : c'est le geste conseillé par
            # l'avertissement « aucun dépôt lisible ».
            review = _resume_review_after_merge(db, item_id, sha=sha, pr_ref=pr_ref,
                                                repo=repo, explicit=retry_review)
            detail = "already %s" % state
            if review.get("created") and review.get("id"):
                detail += " — relecture #%d ouverte" % int(review["id"])
            elif review.get("id"):
                detail += " — relecture #%d déjà ouverte" % int(review["id"])
            elif review.get("already_reviewed"):
                detail += " — avis déjà rendu sur la tête fusionnée"
            else:
                detail += " — relecture non ouverte : %s" % (review.get("why") or "?")
            return {"id": int(item_id), "result": "already", "item": item,
                    "detail": detail, "review_lot": review}
        if state == lifecycle.CLOSED:
            return {"id": int(item_id), "result": "refused", "item": item,
                    "detail": "lot closed (%s): merge seen%s, not reopened"
                              % (item.get("close_reason"), " (%s)" % pr_ref if pr_ref else "")}
        release_landed = None
        if landed is None and item.get("migration") is not None:
            # #4100 : un paquet qui a renuméroté la migration ; le créneau
            # atterri suit le numéro posé, ou l'ancien est relâché — la note
            # le dit
            probe = _landing_probe(db, item, item["migration"], item.get("migration_repo"),
                                   sha, repo, getattr(db, "cfg", None))
            if probe is not None:
                if "landed" in probe:
                    landed = (probe["landed"], probe["repo"])
                else:
                    release_landed = (probe["release"], probe["repo"])
                mention = _landing_note(probe, item["migration"])
                extra = "%s — %s" % (extra, mention) if extra else mention
        note = "merge %s%s%s%s%s%s" % (
            "declared" if declared else "seen",
            " by %s" % source if source else "", " — %s" % pr_ref if pr_ref else "",
            " (commit %s)" % sha[:12] if sha else "", " — %s" % extra if extra else "",
            _connector_mention(connector))
        threads: list = []
        try:
            from . import work_dependencies
            with db.transaction() as tx:
                work_dependencies.lock(tx)
                closure_note = note
                if not storage.is_remote(db):
                    locked = storage.of(tx).work.lock(item_id)
                    if locked is not None:
                        retention = (content_guard(storage.of(tx), locked)
                                     if content_guard is not None else None)
                        if retention is None or retention.get("lift"):
                            # #3022 (canon §5) : un lot de porte de production
                            # ne se ferme que présent sur la branche de prod,
                            # quelle que soit la voie du constat — y compris
                            # quand le contenu lève le hold de l'assigné (#3219).
                            from . import prod_gate
                            gate = prod_gate.check(storage.of(tx), locked, sha=sha,
                                                   repo=repo, cfg=getattr(db, "cfg", None))
                            if gate["retention"] is not None:
                                retention = gate["retention"]
                            if gate["proof"] is not None:
                                closure_note = "%s — %s" % (
                                    closure_note, prod_gate.proof_text(gate["proof"]))
                        if retention is not None:
                            if retention.get("what") == "unreadable":
                                return {"id": int(item_id), "result": "error",
                                        "item": locked, "retention": retention,
                                        "detail": retention.get("motif")}
                            if not retention.get("lift"):
                                return {"id": int(item_id), "result": "withheld",
                                        "item": locked, "retention": retention,
                                        "detail": "content merged, closure withheld: %s"
                                                  % retention.get("motif")}
                            # #3219 : le contenu prouvé lève la retenue
                            # ordinaire de l'assigné ; la note de fusion dit
                            # la levée (le journal garde la trace).
                            closure_note = "%s — %s" % (
                                closure_note, retention.get("motif")
                                or "hold de l'assigné levé par le contenu")
                row = storage.of(tx).work.close_merged(
                    item_id, current=state, sha=sha, actor=actor, note=closure_note,
                    pr_ref=pr_ref, frozen_id=frozen_id, landed=landed,
                    unfrozen=unfrozen, release_landed=release_landed)
                if row is not None:
                    _notify_released(tx, item_id, blocker_state=lifecycle.DELIVERY_RECORDED,
                                     after_commit=threads)
        except DbError as exc:
            # A refused module requirement is a per-lot result, allowing a
            # merge sweep to continue with other projects. Other DB errors
            # remain errors; the storage transaction already rolled back.
            if "AMEESH_MODULE_RULE:" not in str(exc):
                raise
            return {"id": int(item_id), "result": "refused", "item": item,
                    "detail": str(exc)}
        # le fil externe après le commit seulement : un envoi annulé n'y
        # laisse rien
        for record in threads:
            record(db.cfg, db)
        if row is not None:
            # L539 (#1104) : la fusion est écrite ; le lot de relecture suit,
            # sans jamais pouvoir la défaire. Un échec d'ouverture est rendu
            # dans le constat (`review_lot`), pour être repris (`review_lot.catch_up`).
            review = _review_after_merge(db, item_id, sha=sha, pr_ref=pr_ref, repo=repo)
            detail = "%s → merged" % state
            if not review.get("id") and not review.get("already_reviewed"):
                detail += " — relecture non ouverte : %s" % (review.get("why") or "?")
            return {"id": int(item_id), "result": "merged", "item": row,
                    "detail": detail, "review_lot": review}
        if frozen_id is not None or unfrozen:
            freezes = [m for m in milestones(db, item_id) if m["kind"] == "frozen"]
            if (bool(freezes) if frozen_id is None
                    else not freezes or int(freezes[0]["id"]) != int(frozen_id)):
                return {"id": int(item_id), "result": "refrozen", "item": get(db, item_id),
                        "detail": "new freeze declared in the meantime: not closed, review again"}
    raise WorkError("lot %s moved in the meantime: retry" % item_id)


def _declared_merge_commit(item: dict, sha: str, *, repo: str | None = None,
                           cfg=None, announce=None) -> str:
    """#1286 : preuve locale, dans le dépôt du lot, avant toute fermeture.

    `announce` (facultatif, #1400) : appelé avec le constat `{id, app, title,
    sha, subject, how}` une fois la preuve lue et acceptée, AVANT que
    l'appelant n'écrive quoi que ce soit ; un refus de preuve ne l'appelle
    pas.
    """
    from . import plan_git
    from .canon import GitError

    label = "lot #%s (%s: %s)" % (item["id"], item.get("app") or "?", item["title"])
    try:
        slug = lot_repository(item, cfg)
        checkout = _checkout(slug, repo, cfg)
        commit = plan_git._commit(checkout, sha)
        if not commit:
            raise WorkError("commit %s not found in the lot's repository" % sha)
        target = item.get("branch_target") or plan_git.default_target(checkout)
        head = plan_git._target_tip(checkout, target) if target else None
        if not head:
            raise WorkError("merge target %r not found (fetch or configure ameesh.target)"
                            % target)
        if plan_git._git(checkout, "merge-base", "--is-ancestor", commit, head).returncode != 0:
            raise WorkError("commit %s has not entered target %s" % (commit[:12], target))
        message = plan_git._out(checkout, "show", "-s", "--format=%B", commit)
        if message is None:
            raise WorkError("commit message unreadable")
        title = message.splitlines()[0] if message else ""
        lot = int(item["id"])
        if any(int(m.group(1)) == lot for m in plan_git.PARTIAL_TRAILER_RE.finditer(message)):
            raise WorkError("commit declares only a partial delivery of this lot")
        how = plan_git.cited_lots(message, hash_refs=plan_git.lot_ref_hash(checkout)).get(lot)
        if how == "lot-branch" and item.get("branch"):
            cited = plan_git.LOT_BRANCH_MERGE_RE.match(title)
            if cited.group("branch") != item["branch"]:
                how = None
        if not how and item.get("branch"):
            # #1400 : une branche enregistrée du lot (agent/…) est reconnue
            # dans un VRAI commit de fusion — forme GitHub « from
            # <propriétaire>/<branche> » comprise —, à condition que le
            # propriétaire soit celui du dépôt canonique du lot (une branche
            # homonyme d'un fork ne prouve rien) ; une simple mention dans un
            # corps de revue ou un commit ordinaire ne prouve rien.
            parents = plan_git._out(checkout, "show", "-s", "--format=%P", commit)
            if (parents and len(parents.split()) >= 2
                    and plan_git.recorded_branch_merge(
                        title, item["branch"], owner=slug.split("/", 1)[0])):
                how = "recorded branch"
        if not how:
            raise WorkError("commit does not designate this lot: use ameesh-work: %s "
                            "in the commit message (a PR number alone is not a lot)" % lot)
        if announce is not None:
            announce({"id": lot, "app": item.get("app") or "",
                      "title": item.get("title") or "", "sha": commit,
                      "subject": title, "how": how})
        return commit
    except (WorkError, GitError, plan_git.MergeProbeError) as exc:
        raise WorkError("%s: merge declaration refused: %s" % (label, exc)) from exc


def _merged_pr(item: dict, ref: str, *, sha: str = "", db=None, announce=None,
               gh=None) -> tuple[str, str, int | None]:
    """#2603 : preuve d'une PR fusionnée hors du dépôt du lot (canon, home).

    `--pr owner/repo#N` : lit la PR par `gh`, exige l'état MERGED et rend
    `(commit de fusion, référence de la PR, id du gel vérifié)`. La tête de
    la PR doit être le gel du lot (ou, sans gel, sa branche) : une PR
    quelconque ne ferme pas un lot. GitHub doit rendre le commit de fusion,
    et ce commit doit être entré dans la branche par défaut du dépôt de la PR
    (`compare` `behind`/`identical`) : une PR fusionnée sur une base morte
    n'est pas livrée (home#112/#113). `--sha` n'est qu'un contrôle d'égalité.
    Aucune écriture."""
    from . import land, plan_git

    match = _PR_REF_RE.match((ref or "").strip())
    if not match:
        raise WorkError("--pr: owner/repo#N (the merged PR of the lot), not %r" % (ref,))
    slug = match.group("slug")
    number = int(match.group("number"))
    pr_ref = "%s#%d" % (slug, number)
    reader = gh or land._gh
    try:
        proc = reader(["pr", "view", str(number), "--repo", slug, "--json",
                       "number,state,title,mergeCommit,headRefOid,headRefName,"
                       "baseRefName,url"])
    except OSError as exc:
        raise WorkError("--pr %s: `gh pr view` unavailable: %s" % (pr_ref, exc)) from exc
    if proc.returncode != 0:
        raise WorkError("--pr %s: `gh pr view` failed: %s"
                        % (pr_ref, (proc.stderr or proc.stdout or "").strip()[:300]))
    try:
        data = land._json(proc, "gh pr view", dict)
    except Exception as exc:  # noqa: BLE001 - a malformed view proves nothing
        raise WorkError("--pr %s: unreadable PR view: %s" % (pr_ref, exc)) from exc
    state = str(data.get("state") or "").upper()
    if state != "MERGED":
        raise WorkError("--pr %s is %s, not merged: nothing declared"
                        % (pr_ref, state or "?"))
    merge = str(((data.get("mergeCommit") or {}) or {}).get("oid") or "").strip().lower()
    if not merge:
        # #2603 (cx2) : GitHub rend `merge_commit_sha` même en squash/rebase ;
        # un --sha déclaré ne prouve pas, à lui seul, l'entrée dans la cible.
        # Sans commit de fusion, rien n'est déclaré.
        raise WorkError("--pr %s: no merge commit reported by GitHub: nothing declared "
                        "(--sha is only an equality check)" % pr_ref)
    head = str(data.get("headRefOid") or "").strip().lower()
    if not head:
        raise WorkError("--pr %s: unreadable head" % pr_ref)
    frozen = ""
    freeze = None
    if db is not None:
        try:
            freeze = plan_git.latest_freeze(milestones(db, int(item["id"])))
        except Exception as exc:  # noqa: BLE001 - a doubt never closes a lot
            raise WorkError("--pr %s: lot #%s milestones unreadable: %s"
                            % (pr_ref, item["id"], type(exc).__name__)) from exc
        if freeze is not None:
            frozen = str(freeze.get("sha") or "").strip().lower()
            if not frozen:
                raise WorkError("--pr %s: the latest freeze of lot #%s has no commit: "
                                "nothing declared" % (pr_ref, item["id"]))
    if frozen:
        if head != frozen:
            raise WorkError("--pr %s head is %s, the lot is frozen on %s: nothing declared"
                            % (pr_ref, head[:12], frozen[:12]))
    elif item.get("branch"):
        branch = str(data.get("headRefName") or "").strip()
        if branch != item["branch"]:
            raise WorkError("--pr %s head branch is %s, the lot is on %s: nothing declared"
                            % (pr_ref, branch or "?", item["branch"]))
    else:
        raise WorkError("--pr %s: lot #%s has neither a freeze nor a branch to link the "
                        "PR: nothing declared" % (pr_ref, item["id"]))
    if sha and sha != merge:
        raise WorkError("--sha %s differs from the PR merge commit %s: nothing declared"
                        % (sha[:12], merge[:12]))
    # #2603 (cx2 B) : la fusion constatée doit être une livraison réelle dans
    # la cible du dépôt de la PR. Une base morte fusionnée plus tard dans le
    # défaut reste livrée (le commit y est alors ancêtre) ; une base morte qui
    # n'y est pas entrée ne ferme pas le lot (home#112/#113).
    base = str(data.get("baseRefName") or "").strip()
    try:
        proc = reader(["repo", "view", slug, "--json", "defaultBranchRef"])
    except OSError as exc:
        raise WorkError("--pr %s: `gh repo view` unavailable: %s" % (pr_ref, exc)) from exc
    if proc.returncode != 0:
        raise WorkError("--pr %s: cannot read the default branch of %s: %s: nothing declared"
                        % (pr_ref, slug, (proc.stderr or proc.stdout or "").strip()[:300]))
    try:
        repo_view = land._json(proc, "gh repo view", dict)
    except Exception as exc:  # noqa: BLE001 - a malformed view proves nothing
        raise WorkError("--pr %s: unreadable repository view: %s" % (pr_ref, exc)) from exc
    default = str(((repo_view.get("defaultBranchRef") or {}) or {}).get("name")
                  or "").strip()
    if not default:
        raise WorkError("--pr %s: %s reports no default branch: nothing declared"
                        % (pr_ref, slug))
    try:
        proc = reader(["api", "repos/%s/compare/%s...%s"
                       % (slug, quote(default, safe=""), quote(merge, safe=""))])
    except OSError as exc:
        raise WorkError("--pr %s: `gh api compare` unavailable: %s"
                        % (pr_ref, exc)) from exc
    if proc.returncode != 0:
        raise WorkError("--pr %s: comparison of %s with %s unreadable: %s: nothing declared"
                        % (pr_ref, merge[:12], default,
                           (proc.stderr or proc.stdout or "").strip()[:300]))
    try:
        compared = land._json(proc, "gh api compare", dict)
    except Exception as exc:  # noqa: BLE001 - a malformed comparison proves nothing
        raise WorkError("--pr %s: comparison of %s with %s unreadable: %s: nothing declared"
                        % (pr_ref, merge[:12], default, exc)) from exc
    status = str(compared.get("status") or "").strip().lower()
    if status not in ("behind", "identical"):
        raise WorkError(
            "--pr %s: merged into %s, commit %s is not in %s (%s): content not delivered "
            "(home#112/#113); nothing declared"
            % (pr_ref, base or "?", merge[:12], default, status or "unknown"))
    if announce is not None:
        announce({"id": int(item["id"]), "app": item.get("app") or "",
                  "title": item.get("title") or "", "sha": merge,
                  "subject": str(data.get("title") or ""),
                  "how": "merged PR %s" % pr_ref})
    return merge, pr_ref, int(freeze["id"]) if freeze is not None else None


def _merge_proof_repo(item: dict, repo: str | None, cfg=None, *,
                      pr_ref: str | None = None) -> str:
    """#3201 : le clone local où la fusion a été prouvée, indice de classe.

    `merged()` le connaissait au moment de la preuve — le clone vérifié par
    `_declared_merge_commit` pour une déclaration `--sha`, le dépôt de la PR
    pour `--pr` — et le perdait en appelant `close_merged` : sur un hôte sans
    le dossier de travail de l'assigné ni le projet radar (le rattrapage d'un
    exécuteur), `class_at_merge` rendait « dépôt inconnu » et la relecture §2
    d'un lot léger ne s'ouvrait jamais (#3029, #3011, #3105). Best effort :
    sans clone vérifiable, la chaîne vide laisse à `class_at_merge` ses replis
    (dossier de l'assigné, dépôt du projet) et son doute conservateur.
    """
    slugs: list[str] = []
    if pr_ref:
        slug = str(pr_ref).rpartition("#")[0].strip()
        if "/" in slug:
            slugs.append(slug)
    try:
        slug = lot_repository(item, cfg)
    except WorkError:
        slug = ""
    if slug and slug not in slugs:
        slugs.append(slug)
    for slug in slugs:
        # le chemin donné ne vaut que pour son dépôt (une PR du canon pour un
        # lot d'ameesh) : `_checkout` le refuse par son `origin`, puis le clone
        # déclaré de l'hôte est essayé
        for path in (repo, None):
            try:
                return _checkout(slug, path, cfg)
            except Exception:  # noqa: BLE001 - indice de classe, jamais la fusion
                continue
    return ""


def merged(db: Db, item_id: int, *, sha: str = "", note: str = "", actor: str = "",
           repo: str | None = None, migration_landed: int | None = None, cfg=None,
           announce=None, pr: str | None = None) -> dict:
    """`ameesh work merged` : la livraison `git-merge` de `deliver` (L99 T2),
    preuve `{sha, pr}` ; voir `record_merge`."""
    evidence = {"sha": sha}
    if pr is not None:
        evidence["pr"] = pr
    return deliver(db, item_id, connector=merge_connector(), evidence=evidence,
                   actor=actor, note=note, repo=repo, migration_landed=migration_landed,
                   cfg=cfg, announce=announce)


def record_merge(db: Db, item_id: int, *, sha: str = "", note: str = "", actor: str = "",
                 repo: str | None = None, migration_landed: int | None = None, cfg=None,
                 announce=None, pr: str | None = None) -> dict:
    """`ameesh work merged <id> --sha S | --pr owner/repo#N` : une fusion faite
    hors PR et sans gel (fusion locale sur la branche cible), ou une PR
    fusionnée hors du dépôt du lot (#2603), se déclare en une fois.

    Le lot passe `merged` depuis tout état ouvert (intake, build, qa,
    blocked, waiting_human) ; son jalon `merged` reçoit le commit de fusion et
    l'acteur ; le journal dit « fusion déclarée par <acteur> (commit …) ».
    Idempotent (`already`, rien n'est réécrit) ; un lot fermé (abandonné,
    remplacé) n'est jamais rouvert : WorkError. Rend le constat de
    `close_merged`. Le commit doit être lisible dans le dépôt du lot, désigner
    ce lot et être entré dans sa cible ; aucune lecture réseau ni force.

    `pr="owner/repo#N"` (#2603) : la preuve est la PR fusionnée d'un AUTRE
    dépôt que celui du lot (une PR du canon pour un lot d'ameesh) ; sa tête
    doit être le gel du lot, GitHub doit rendre son commit de fusion, et ce
    commit doit être entré dans la branche par défaut du dépôt de la PR
    (`compare` `behind`/`identical`, un doute refuse) ; `--sha` n'est qu'un
    contrôle d'égalité avec ce commit. La lecture de la PR passe par `gh` ;
    aucune écriture avant le constat.

    `announce` (facultatif, #1400) : le callback du constat, appelé avec
    `{id, app, title, sha, subject, how}` après la lecture de la preuve (une
    branche enregistrée est reconnue dans un vrai commit de fusion, GitHub
    comprise) et AVANT toute écriture ; un refus de preuve ne l'appelle pas, un
    refus d'écriture (gel déclaré entre-temps) l'a déjà appelé et laisse
    l'état inchangé.

    `migration_landed` (#1567) : le numéro de migration réellement atterri,
    quand la renumérotation du paquet a changé celui de la réservation (452 au
    lieu de 457). Il est écrit dans la transaction de la fusion ; sur un lot
    déjà fusionné ou promu, il enregistre (ou corrige) le fait après coup,
    journalisé.

    #3201 : le clone où la preuve a été lue (`repo`, ou le dépôt de `--pr`)
    est transmis à `close_merged` (`repo`) et sert d'indice de classe à la
    relecture §2 ; sans lui, un hôte qui ne connaît ni le dossier de
    l'assigné ni le projet radar rendait « dépôt inconnu » et la relecture
    d'un lot léger ne s'ouvrait jamais. Le même dépôt rejoue une relecture
    manquante sur un lot déjà fusionné (dans les bornes du rattrapage).

    #3413 : ce rejeu est **explicite** — le clone fourni relance la
    classification sans attendre le délai ni le plafond de tentatives du
    balayage, sinon l'avertissement « aucun dépôt lisible » conseillerait un
    geste qui répond « already merged: nothing to do ». La borne d'activation
    du rattrapage (fusion antérieure non relue) reste appliquée."""
    pr_text = (pr or "").strip()
    retry_review = bool((repo or "").strip() or pr_text)
    text = (sha or "").strip().lower()
    if not text and not pr_text:
        raise WorkError("--sha: the merge commit (7 to 64 hex characters); for a PR of "
                        "another repository, give --pr owner/repo#N")
    if text and not _SHA_RE.match(text):
        raise WorkError("--sha: the merge commit (7 to 64 hex characters), not %r"
                        % (sha,))
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    pr_ref = None
    proof_guard = {}
    proof_repo = ""
    # Preserve idempotency and the closed-lot diagnostic without requiring
    # that an old clone or object still be available.
    fence = None
    if item["state"] not in (*MERGED_STATES, lifecycle.CLOSED):
        # L165 (EP4) : déclarer une fusion faite à la main est un geste du
        # pilote hors base (le constat ferme le lot). La garde ne fait que lire
        # ici : la preuve peut être longue (GitHub), le hold et l'écriture de
        # `close_merged` sont dans la même transaction plus bas (revue cx2 B3).
        from . import pilot_gesture
        fence = pilot_gesture.gate(cfg, db, "merge", "merged #%d" % item_id,
                                   project=(item.get("app") or None))
        if pr_text:
            text, pr_ref, frozen_id = _merged_pr(
                item, pr_text, sha=text, db=db, announce=announce)
            proof_guard = {"frozen_id": frozen_id, "unfrozen": frozen_id is None}
            proof_repo = _merge_proof_repo(item, repo, cfg, pr_ref=pr_ref)
        else:
            text = _declared_merge_commit(item, text, repo=repo, cfg=cfg, announce=announce)
            proof_repo = _merge_proof_repo(item, repo, cfg)
    else:
        # #3201 : un lot déjà fusionné peut rejouer sa relecture manquante ; le
        # dépôt donné (ou celui de la PR) reste l'indice de classe.
        proof_repo = _merge_proof_repo(item, repo, cfg, pr_ref=pr_text or None)
    landed = None
    if migration_landed is not None:
        number = _landed_number(migration_landed)
        landed_repo = _landed_repo(item, cfg)
        _check_landed_free(db, item_id, number, landed_repo, cfg)
        landed = (number, landed_repo)
    if landed is not None and item["state"] in MERGED_STATES:
        return _record_landed(db, item, landed, actor=actor, cfg=cfg)
    if landed is None and item["state"] in MERGED_STATES and (repo or "").strip() and text:
        # #4100 : rattrapage d'un lot fusionné avant le suivi automatique
        repaired = _repair_landing(db, item, text, repo.strip(), actor=actor, cfg=cfg)
        if repaired is not None:
            return repaired
    extra = (note or "").strip()
    if landed is not None:
        mention = "migration %s landed" % migration_layout(
            item, cfg, repo=landed[1]).label(landed[0])
        if item.get("migration") is not None and int(item["migration"]) != landed[0]:
            mention += " (reserved %s)" % migration_layout(item, cfg).label(
                item["migration"])
        extra = "%s — %s" % (extra, mention) if extra else mention
    try:
        if fence is None:
            done = close_merged(db, item_id, sha=text, actor=actor,
                                source=actor or UNKNOWN_ACTOR, declared=True, extra=extra,
                                landed=landed, pr_ref=pr_ref, repo=proof_repo,
                                retry_review=retry_review, connector=merge_connector(),
                                **proof_guard)
        else:
            with db.transaction() as tx:
                fence.hold(tx)
                done = close_merged(tx, item_id, sha=text, actor=actor,
                                    source=actor or UNKNOWN_ACTOR, declared=True,
                                    extra=extra, landed=landed, pr_ref=pr_ref,
                                    repo=proof_repo, retry_review=retry_review,
                                    connector=merge_connector(), **proof_guard)
    except DbError as exc:
        # #1567 : course perdue sur l'index unique (dépôt, numéro atterri) —
        # message lisible plutôt que la DbError brute ; la pré-vérification a
        # couvert le cas ordinaire
        if landed is None or not is_unique_violation(exc, _pg.MIGRATION_LANDED_INDEX):
            raise
        holder = storage.of(db).work.migration_landed_holder(*landed)
        raise WorkError(_landed_taken(landed[0], landed[1], holder, cfg)) from exc
    if landed is not None and done["result"] == "already":
        # un balayage a pu fusionner le lot après notre lecture : le numéro
        # déclaré est enregistré (ou corrige celui déjà tenu) après coup
        return _record_landed(db, done["item"], landed, actor=actor, cfg=cfg)
    if done["result"] == "refrozen":
        raise WorkError(done["detail"])
    if done["result"] == "refused":
        item = done["item"]
        if item["state"] != lifecycle.CLOSED:
            raise WorkError(done["detail"])
        raise WorkError("lot %s closed (%s): not reopened; see ameesh work show %s"
                        % (item_id, close_how(item.get("close_reason"),
                                              item.get("superseded_by")), item_id))
    if text:
        done["sha"] = text
    return done


def close_label(item: dict) -> str:
    """Le libellé d'une fermeture (`closed`), pour les messages : abandonné,
    remplacé par #N, livré hors cible (#988), ou `done` (#1280)."""
    return close_how(item.get("close_reason"), item.get("superseded_by"))


def _landed_number(number) -> int:
    value = int(number)
    if not 1 <= value <= 9999:
        raise WorkError("migration number outside 0001-9999: %s" % number)
    return value


def _landing_probe(db: Db, item: dict, reserved, slug, sha: str, repo: str | None,
                   cfg=None) -> dict | None:
    """#4100 : le numéro qu'un paquet a réellement posé pour la migration
    réservée `reserved` (dépôt `slug`) du lot, lu dans le clone `repo`.

    Le constat de fusion d'un balayage (paquet fermé par `sync-packets`,
    branche entrée dans sa cible par `sync-branches`) ne déclare pas
    `migration_landed` : le créneau atterrissait au numéro réservé, même quand
    le paquet l'avait renuméroté (#4062 600 → 595, #4079 602 → 599). `sha`
    est la PREUVE de livraison : pour une composition, l'unité du paquet, déjà
    renumérotée (revue cx3). Le fichier `<N>_<nom>.sql` du lot est donc lu à
    sa SOURCE, son dernier gel, puis à défaut au commit de preuve ; s'il est
    sur la cible tel quel, rien à faire (None). Sinon la cible est cherchée
    par le nom après le numéro, puis, à défaut, par l'empreinte du contenu
    (blob). Sans fichier identifiable, une unité qui
    ajoute un seul fichier numéroté présent sur la cible le désigne
    (commit simple ou fusion, comparé à sa première parente) :
    - un seul candidat M → `{"landed": M, "repo": slug, "how": …}` ;
    - aucun, plusieurs, ou M déjà nommé par un autre lot →
      `{"release": N, "repo": slug, "why": …}` : l'ancien numéro est relâché,
      jamais deviné.
    Une lecture impossible (autre dépôt, commit ou cible illisible) rend None :
    le constat de fusion n'en dépend jamais."""
    from . import migrations

    if reserved is None or not slug or not repo or not sha:
        return None
    reserved = int(reserved)
    try:
        target = _migration_target(item, repo, cfg)
        layout = migrations.layout_for(slug, cfg)
        if not target:
            return None
        origin = migrations.origin_repo(repo)
        if layout is None or not origin or origin.lower() != str(slug).lower():
            return None
        if not migrations._git(repo, "rev-parse", "--verify", "--quiet",
                               "%s^{commit}" % sha).strip():
            return None
        resolved = migrations.target_commit(repo, target)
        if resolved is None:
            return None
        present = migrations.tree_files(repo, layout, resolved[1])
        # les fichiers du lot : ceux que la cible n'avait pas quand le lot y
        # est entré (premier parent du commit de fusion sur la ligne de la
        # cible ; la base commune pour une composition ou un squash)
        def number(name: str) -> int:
            return int(migrations._REPO_FILE_RE.match(name).group(1))

        def rest(name: str) -> str:
            return name[migrations._REPO_FILE_RE.match(name).end():]

        ours: dict[str, str] = {}
        for source in _lot_sources(db, item, sha, repo):
            numbered = {name: blob for name, blob
                        in migrations.tree_files(repo, layout, source).items()
                        if number(name) == reserved}
            base = _merge_base_before(repo, source, resolved[1])
            if base and base == source:
                # revue cx2 : la source est sur la ligne de premier parent de
                # la cible (avance rapide) — aucune base ne sépare le lot ; un
                # fichier N encore sur la cible est posé tel quel, celui que la
                # cible n'a plus a été renuméroté depuis
                if any(name in present for name in numbered):
                    return None
                ours = numbered
            else:
                before = migrations.tree_files(repo, layout, base) if base else {}
                ours = {name: blob for name, blob in numbered.items()
                        if name not in before}
            if ours:
                break
        if any(name in present for name in ours):
            return None
        on_target = {number(name) for name in present}

        def by_unit() -> dict | None:
            unit = _unit_added(repo, layout, sha)
            posed = {number(name) for name in unit if name in present}
            if len(posed) == 1 and len(unit) == 1 and reserved not in posed:
                landed = posed.pop()
                holder = storage.of(db).work.migration_landed_holder(landed, slug)
                if holder is None or int(holder["id"]) == int(item["id"]):
                    return {"landed": landed, "repo": slug,
                            "how": "%s added by unit %s" % (unit[0], sha[:12])}
            return None

        if not ours:
            unit = by_unit()
            if unit is not None:
                return unit
            if reserved in on_target:
                return None   # le numéro porte un autre fichier : rien à dire
            return {"release": reserved, "repo": slug,
                    "why": "no file %s_* in commit %s nor on %s" % (reserved, sha[:12], target)}
        if len(ours) > 1:
            return {"release": reserved, "repo": slug,
                    "why": "several files numbered %s in commit %s" % (reserved, sha[:12])}
        (name, blob), = ours.items()
        how = "name"
        found = {number(other) for other in present
                 if rest(other) == rest(name) and number(other) != reserved}
        if not found:
            how = "content"
            found = {number(other) for other, other_blob in present.items()
                     if other_blob == blob and number(other) != reserved}
        unit = None if found else by_unit()
        if unit is not None:
            return unit
        if len(found) != 1:
            return {"release": reserved, "repo": slug,
                    "why": "%s absent from %s, %s" % (
                        name, target, "no renumbered match" if not found else
                        "ambiguous matches %s" % ", ".join(str(n) for n in sorted(found)))}
        landed = found.pop()
        holder = storage.of(db).work.migration_landed_holder(landed, slug)
        if holder is not None and int(holder["id"]) != int(item["id"]):
            return {"release": reserved, "repo": slug,
                    "why": "%s absent from %s, %s already landed by #%s" % (
                        name, target, landed, holder["id"])}
        return {"landed": landed, "repo": slug,
                "how": "%s → %s (by %s)" % (name, landed, how)}
    except (OSError, ValueError, AttributeError, RuntimeError):
        return None


def _repair_landing(db: Db, item: dict, sha: str, repo: str, *, actor: str,
                    cfg=None) -> dict | None:
    """#4100 : un lot déjà fusionné dont le créneau atterri tient encore le
    numéro réservé (fusion constatée avant le suivi automatique) passe par le
    même relevé que la fermeture : numéro posé enregistré, ou ancien numéro
    relâché, journalisé. None quand il n'y a rien à dire (numéro atterri déjà
    déclaré, aucun créneau atterri, fichier posé tel quel, lecture impossible)."""
    if item.get("migration_landed") is not None:
        return None
    slot = storage.of(db).work.landed_slot(int(item["id"]))
    if slot is None:
        return None
    probe = _landing_probe(db, item, slot["number"], slot["repo"], sha, repo, cfg)
    if probe is None:
        return None
    if "landed" in probe:
        return _record_landed(
            db, dict(item, migration=slot["number"], migration_repo=slot["repo"]),
            (probe["landed"], probe["repo"]), actor=actor, cfg=cfg)
    note = _landing_note(probe, slot["number"])
    row = storage.of(db).work.release_landed_slot(int(item["id"]), probe["release"],
                                                  probe["repo"], note=note, actor=actor)
    if row is None:
        return None
    return {"id": int(item["id"]), "result": "landed", "item": row, "detail": note}


def _lot_sources(db: Db, item: dict, sha: str, repo: str) -> list[str]:
    """#4100 (revue cx3) : les commits où lire les fichiers du lot — son
    dernier gel s'il est lisible dans le clone (la source, avant toute
    renumérotation d'un paquet), la branche fusionnée par une unité
    `merge --no-ff` (seconde parente, revue dev3), puis le commit de preuve."""
    from . import migrations, plan_git

    sources: list[str] = []
    try:
        freeze = plan_git.latest_freeze(
            storage.of(db).progress.lot_milestones([int(item["id"])]))
    except (DbError, AttributeError, KeyError):
        freeze = None
    frozen = ((freeze or {}).get("sha") or "").strip()
    if frozen:
        frozen = migrations._git(repo, "rev-parse", "--verify", "--quiet",
                                 "%s^{commit}" % frozen).strip()
    merged = migrations._git(repo, "rev-parse", "--verify", "--quiet",
                             "%s^2^{commit}" % sha).strip()
    for candidate in (frozen, merged, sha):
        if candidate and candidate not in sources:
            sources.append(candidate)
    return sources


def _unit_added(repo: str, layout, sha: str) -> list[str]:
    """#4100 : les fichiers numérotés qu'ajoute l'unité `sha` à la cible, par
    rapport à sa première parente — un commit simple ou une fusion
    `--no-ff` de `paquet-compose.py` (revue dev3) ; vide pour une racine."""
    from . import migrations

    parents = migrations._git(repo, "rev-list", "--parents", "-n", "1", sha).split()
    if len(parents) < 2:
        return []
    before = migrations.tree_files(repo, layout, parents[1])
    return sorted(name for name in migrations.tree_files(repo, layout, sha)
                  if name not in before)


def _merge_base_before(repo: str, sha: str, target: str) -> str:
    """#4100 : la tête de la cible juste avant l'entrée de `sha` — la base
    commune quand `sha` n'est pas ancêtre (composition, squash), sinon la base
    avec le premier parent du commit qui l'a fait entrer sur la ligne de
    premier parent de la cible. Vide si illisible."""
    from . import migrations

    if not _is_ancestor(repo, sha, target):
        return migrations._git(repo, "merge-base", sha, target).strip()
    line = migrations._git(repo, "rev-list", "--first-parent", "--ancestry-path",
                           "%s..%s" % (sha, target)).split()
    if not line:
        return ""
    return migrations._git(repo, "merge-base", "%s^1" % line[-1], sha).strip()


def _is_ancestor(repo: str, sha: str, target: str) -> bool:
    import subprocess

    try:
        return subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", sha, target],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _landing_note(probe: dict, reserved) -> str:
    """#4100 : la ligne de journal du numéro suivi ou relâché."""
    if "landed" in probe:
        return "migration %s landed (reserved %s, renumbered: %s)" % (
            probe["landed"], reserved, probe["how"])
    return "migration %s released (not landed: %s)" % (probe["release"], probe["why"])


def _landed_repo(item: dict, cfg=None) -> str:
    """Le dépôt du numéro atterri : celui de la réservation, sinon celui déjà
    enregistré, sinon celui du lot (son issue, ou son projet déclaré à l'hôte)."""
    return (item.get("migration_repo") or item.get("migration_landed_repo")
            or lot_repository(item, cfg))


def _check_landed_free(db: Db, item_id: int, number: int, repo: str, cfg=None) -> None:
    holder = storage.of(db).work.migration_landed_holder(number, repo)
    if holder is not None and int(holder["id"]) != int(item_id):
        raise WorkError(_landed_taken(number, repo, holder, cfg))


def _landed_taken(number: int, repo: str, holder: dict | None, cfg=None) -> str:
    who = ("lot #%s (%s)" % (holder["id"], holder["title"]) if holder
           else "a concurrent record")
    return "landed migration %s of %s already named by %s" % (
        migration_layout({}, cfg, repo=repo).label(number), repo, who)


def _record_landed(db: Db, item: dict, landed: tuple[int, str], *, actor: str,
                   cfg=None) -> dict:
    """#1567 : enregistre (ou corrige) le numéro atterri d'un lot déjà fusionné."""
    number, repo = landed
    previous = item.get("migration_landed")
    if (previous is not None and int(previous) == number
            and item.get("migration_landed_repo") == repo):
        return {"id": int(item["id"]), "result": "already", "item": item,
                "detail": "already %s" % item["state"]}
    label = migration_layout({}, cfg, repo=repo).label(number)
    if previous is None:
        text = "landed migration %s recorded (%s)" % (label, repo)
        reserved = item.get("migration")
        if reserved is not None and int(reserved) != number:
            text += " — reserved %s" % migration_layout(item, cfg).label(reserved)
    else:
        old = migration_layout({}, cfg, repo=item.get("migration_landed_repo") or repo)
        text = "landed migration %s → %s corrected (%s)" % (
            old.label(previous), label, repo)
    try:
        row = storage.of(db).work.record_landed_migration(int(item["id"]), number, repo,
                                                          note=text, actor=actor)
    except DbError as exc:
        if not is_unique_violation(exc, _pg.MIGRATION_LANDED_INDEX):
            raise
        holder = storage.of(db).work.migration_landed_holder(number, repo)
        raise WorkError(_landed_taken(number, repo, holder, cfg)) from exc
    if row is None:
        raise WorkError("lot %s is no longer merged: no landed migration" % item["id"])
    return {"id": int(item["id"]), "result": "landed", "item": row,
            "detail": "%s — file %s" % (text, _migration_file(number, repo, cfg))}


def deliver(db: Db, item_id: int, *, connector: str, evidence: dict | None = None,
            actor: str = "", **options) -> dict:
    """L99 T2 (#4350) : le verbe unique de toute livraison d'un lot.

    `connector` choisit la voie et sa preuve (`evidence`) :
    - `git-merge` (module `dev`, lu dans le registre) : une fusion sur la cible,
      `{sha, pr}` déclarée (`ameesh work merged`) ou `{action}` confirmée par
      la porte ; rend le constat de `close_merged` ;
    - `document` (cœur) : livré hors cible, `{sha: <tête relue>}`
      (`close --delivered`) ; rend la ligne fermée ;
    - `done` (cœur) : livré sans fusion, `{}` (`close --done`).
    `options` sont celles de la voie (`note`, `repo`, `cfg`…). Le journal du
    lot nomme le connecteur ; aucun nouveau stockage."""
    name = str(connector or "").strip()
    core = {"document": _deliver_document, "done": _deliver_done}
    if name in core:
        handler = core[name]
    else:
        from .modules import registry
        found = registry.delivery_connectors().get(name)
        if found is None:
            raise WorkError("unknown delivery connector %r (known: %s)" % (
                connector, ", ".join(sorted([*core, *registry.delivery_connectors()]))))
        handler = found[1]
    if evidence is not None and not isinstance(evidence, dict):
        raise WorkError("delivery evidence must be a mapping, not %r" % (evidence,))
    return handler(db, item_id, evidence=dict(evidence or {}), actor=actor, **options)


def merge_connector() -> str:
    """Le connecteur d'une fusion sur la cible (`git-merge`), lu dans le registre."""
    from .modules import registry
    return registry.merge_connector()


def _connector_mention(connector: str) -> str:
    """La trace du connecteur dans une note du journal (L99 T2)."""
    return " [via %s]" % connector if connector else ""


def _deliver_document(db: Db, item_id: int, *, evidence: dict, actor: str = "",
                      note: str = "") -> dict:
    """Connecteur `document` : livré hors cible, la tête relue en preuve (#988)."""
    sha = evidence.pop("sha", "")
    if evidence:
        raise WorkError("document: unknown evidence %s (sha only)"
                        % ", ".join(sorted(evidence)))
    if not _SHA_RE.match((sha or "").strip().lower()):
        raise WorkError("--delivered: the reviewed head (--sha, 7 to 64 hex characters), "
                        "not %r" % (sha,))
    return _close(db, item_id, delivered=True, sha=sha, note=note, actor=actor,
                  connector="document")


def _deliver_done(db: Db, item_id: int, *, evidence: dict, actor: str = "",
                  note: str = "") -> dict:
    """Connecteur `done` : livré sans fusion (un avis, une analyse, #1280)."""
    if evidence:
        raise WorkError("done: takes no evidence, not %s" % ", ".join(sorted(evidence)))
    return _close(db, item_id, done=True, note=note, actor=actor, connector="done")


def close(db: Db, item_id: int, *, abandoned: bool = False, superseded_by: int | None = None,
          delivered: bool = False, done: bool = False, sha: str = "", note: str = "",
          actor: str = "") -> dict:
    """Ferme un lot qui ne sera pas fusionné : abandonné, remplacé par un
    autre lot, **livré hors cible** (#988 : un rapport, une note, un livrable
    dont la tête relue reste sur sa branche) ou `done` — livré sans fusion
    (un avis, une analyse).

    Les quatre portes sont exclusives. `--delivered` exige `--sha <tête relue>`
    (7 à 64 caractères hexadécimaux) : le jalon `delivered` porte ce SHA et
    compte comme livraison dans `estimates`. `done` refuse un lot qui porte
    une branche ou une PR : un lot de code se fusionne, il ne se clôt pas. Il
    refuse aussi un avis (`review-after-merge:#N`,
    le titre `REVUE #N` des relectures ouvertes à la main, ou la mission
    manuelle `R1`/`R2 <harnais> #<parent>`, cas #1249) dont le parent est
    introuvable, et ne ferme un `merged` sans branche que si c'est un avis
    rattaché (les fausses fusions historiques). Le titre rattache, il ne
    prouve pas : la clôture `done` d'un avis exige le jalon `verdict` sur le
    parent relu, rendu par le lecteur de la mission (son assigné, ou un
    relecteur désigné) — quel qu'en soit le verdict, un `blocked` compris, qui
    est une relecture livrée et pas un GO produit — sans ce verdict, la
    relecture n'a pas eu lieu (arbitrage mesh-design 34598, 14/10 ; lecteur
    lié à la mission, R codex3 #1280 B1). Sur un
    avis déjà fermé « superseded by » son propre parent (#1043, #1218, #1249,
    #1269), `done` rectifie la raison sans rouvrir : la fermeture, sa date et
    son jalon restent, la rectification est journalisée. Jalon `closed` ; rien
    ne reste ouvert par oubli.

    Un lot de relecture après fusion (`issue_ref = review-after-merge:#<lot>`)
    n'est livrable que lorsque les avis dus sur la fusion sont rendus : la
    marque structurée de la relecture (au journal du lot fusionné) est relue
    et les verdicts `ok` du lot source comptés à son sha
    (`review_lot.guard_delivery`, #2374). Un lot antérieur, sans marque, garde
    l'avis historique (un).

    Dans la MÊME transaction, les agents des lots qui déclarent ce lot en
    prérequis explicite et dont c'était la dernière attente reçoivent un
    courrier durable (L385 suite) : l'abandon libère l'attente sans prouver la
    livraison, le dépendant doit réévaluer son prérequis. Un échec du courrier
    annule la fermeture (reprise) ; le verrou de graphe empêche deux
    fermetures de se doubler."""
    picks = sum((int(bool(abandoned)), int(superseded_by is not None),
                 int(bool(delivered)), int(bool(done))))
    if picks != 1:
        raise WorkError("pass one of --abandoned, --superseded-by <id>, "
                        "--delivered --sha <reviewed head>, --done")
    head = (sha or "").strip().lower()
    if not delivered and head:
        raise WorkError("--sha only makes sense with --delivered")
    if delivered:
        return deliver(db, item_id, connector="document", evidence={"sha": sha},
                       actor=actor, note=note)
    if done:
        return deliver(db, item_id, connector="done", evidence={}, actor=actor, note=note)
    return _close(db, item_id, abandoned=abandoned, superseded_by=superseded_by,
                  note=note, actor=actor)


def _close(db: Db, item_id: int, *, abandoned: bool = False,
           superseded_by: int | None = None, delivered: bool = False, done: bool = False,
           sha: str = "", note: str = "", actor: str = "", connector: str = "") -> dict:
    """Le corps de `close` : une porte, déjà choisie et validée par l'appelant
    (`close`, ou les connecteurs `document` et `done` de `deliver`)."""
    head = (sha or "").strip().lower()
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    pinned = _pinned_ref(item)
    if pinned:
        # #1922 : la fermeture d'un lot sous workflow passe par le registre.
        raise _pinned_close_error(item_id, pinned)
    if superseded_by is not None:
        if int(superseded_by) == int(item_id):
            raise WorkError("a lot cannot supersede itself")
        if get(db, superseded_by) is None:
            raise WorkError("superseding lot %s not found" % superseded_by)
    state = item["state"]
    review, parent = _review_parent(item)
    if state == lifecycle.CLOSED:
        row = _rectify_done(db, item, review=review, parent=parent, done=done,
                            note=note, actor=actor)
        if row is not None:
            return row
        raise WorkError("lot %s already closed: nothing to close" % item_id)
    if done:
        artifact = _code_artifact(item)
        if artifact:
            raise WorkError("lot %s carries %s: a code lot is merged, not closed "
                            "done" % (item_id, artifact))
        if review and (parent is None or get(db, parent) is None):
            where = ("#%s" % parent) if parent is not None else (
                item.get("issue_ref") or item.get("source") or "?")
            raise WorkError("review #%s not attached to its parent (%s): nothing "
                            "to close done" % (item_id, where))
        if review and not _parent_verdict(db, parent, item):
            raise WorkError("review #%s: no verdict milestone on its parent #%s from its "
                            "reader (%s) — the title attaches, it does not prove; record "
                            "the verdict on the parent first (ameesh work milestone %s "
                            "verdict ok --sha <sha>)"
                            % (item_id, parent, _reader_label(item), parent))
        if state in MERGED_STATES and not review:
            raise WorkError("lot %s already %s, without branch or review parent: "
                            "nothing to close done" % (item_id, state))
    elif state in MERGED_STATES:
        raise WorkError("lot %s already %s: nothing to close" % (item_id, state))
    if delivered or done:
        # #2374 : la clôture d'une relecture après fusion n'est pas un simple
        # constat — les avis dus sur la fusion sont comptés avant d'écrire.
        # La garde vaut aussi pour `done` : le corps de la mission recommande
        # `close --done`, et un avis unique, périmé ou bloqué ne prouve pas une
        # relecture livrée (relecture ameesh-cx1 #2402 B1, 18/10 08:12Z).
        # Hors `review-after-merge:#N`, `guard_delivery` ne rend rien : les
        # missions manuelles `R1`/`R2` gardent la preuve `_parent_verdict`.
        from . import review_lot
        review_lot.guard_delivery(db, item)
    if abandoned:
        reason, base = "abandoned", "abandoned"
    elif superseded_by is not None:
        reason, base = "superseded", "superseded by #%s" % superseded_by
    elif delivered:
        reason, base = "delivered", "delivered (head %s)" % head[:12]
    else:
        reason, base = "done", "done"
    text = (base + (" — %s" % note.strip() if note and note.strip() else "")
            + _connector_mention(connector))
    from . import work_dependencies
    threads: list = []
    with db.transaction() as tx:
        work_dependencies.lock(tx)
        row = storage.of(tx).work.close(
            item_id, current=state, reason=reason,
            superseded_by=int(superseded_by) if superseded_by is not None else None,
            sha=head, actor=actor, note=text)
        if row is not None:
            _notify_released(tx, item_id, blocker_state=lifecycle.CLOSED, blocker_reason=reason,
                             after_commit=threads)
    # le fil externe après le commit seulement : un envoi annulé n'y laisse rien
    for record in threads:
        record(db.cfg, db)
    if row is None:
        # #1922/N14 : le refus peut venir d'une adoption concurrente, que le
        # filtre SQL a interceptée (N8) ; nommer la retenue, pas l'état.
        pinned = _adopted_ref(db, item_id)
        if pinned:
            raise _pinned_close_error(item_id, pinned)
        raise WorkError("lot %s moved in the meantime: retry" % item_id)
    return row


def _code_artifact(item: dict) -> str:
    """#1280 : ce qui rattache un lot au code — `branch <nom>` ou `PR <réf>` —
    ou "" : un lot qui en porte un se fusionne, il ne se clôt pas `done`."""
    branch = str(item.get("branch") or "").strip()
    if branch:
        return "branch %s" % branch
    pr = str(item.get("pr_ref") or "").strip()
    return "PR %s" % pr if pr else ""


def _rectify_done(db: Db, item: dict, *, review: bool, parent: int | None, done: bool,
                  note: str, actor: str) -> dict | None:
    """#1280 : régularise en `done` un avis fermé « superseded by » son propre
    parent relu — le parent n'est pas un remplaçant (#1043, #1269). Rend la
    ligne rectifiée, ou None si le lot n'est pas dans ce cas exact : la
    fermeture, sa date et son jalon restent en place, seule la raison change."""
    if not done or not review or parent is None:
        return None
    artifact = _code_artifact(item)
    if artifact:
        raise WorkError("lot %s carries %s: a code lot is merged, not closed done"
                        % (item["id"], artifact))
    current = item.get("superseded_by")
    if (str(item.get("close_reason") or "") != "superseded" or current is None
            or int(current) != int(parent)):
        return None
    if not _parent_verdict(db, parent, item):
        raise WorkError("review #%s: no verdict milestone on its parent #%s from its "
                        "reader (%s) — the rectification to done requires the delivered "
                        "verdict" % (item["id"], parent, _reader_label(item)))
    item_id = int(item["id"])
    text = ("done (rectification: closed `superseded by #%d`, the reviewed parent, "
            "not a replacement)" % parent)
    if note and note.strip():
        text += " — %s" % note.strip()
    row = storage.of(db).work.rectify_close(
        item_id, current_reason="superseded", reason="done", actor=actor, note=text)
    if row is None:
        raise WorkError("lot %s changed in the meantime: retry" % item_id)
    return row


def close_how(reason: str | None, superseded_by: int | None = None) -> str:
    """Le libellé d'une fermeture : `done` (livré sans fusion), `superseded by
    #N`, `delivered outside the target` (#988), ou `abandoned` — jamais
    « superseded by #None »."""
    if reason == "done":
        return "done (delivered without merge)"
    if reason == "delivered":
        return "delivered outside the target"
    if reason == "superseded":
        return ("superseded by #%s" % superseded_by) if superseded_by is not None \
            else "superseded"
    return "abandoned"


def _mission_readers(item: dict) -> list[str]:
    """Les lecteurs qui peuvent prouver une mission : son assigné, puis les
    relecteurs désignés (#2076). L'ensemble est la preuve ; un verdict d'un
    autre agent ne prouve pas que *cette* mission a été rendue (R codex3
    #1280, B1 : mission assignée à un lecteur, verdict d'un autre)."""
    names = []
    assignee = str(item.get("assignee") or "").strip()
    if assignee:
        names.append(assignee)
    from .work_reviewers import designated
    names.extend(designated(item))
    return list(dict.fromkeys(name for name in names if name))


def _reader_label(item: dict) -> str:
    """Le(s) lecteur(s) attendu(s) d'une mission, pour un message de refus."""
    return ", ".join(_mission_readers(item)) or "aucun lecteur assigné"


def _parent_verdict(db: Db, parent: int | None, item: dict | None = None) -> bool:
    """Le parent relu porte-t-il un jalon `verdict` **du lecteur de cette
    mission**, rendu **après son ouverture** ? Le titre d'un avis rattache le
    parent, il ne prouve pas que la relecture a été rendue ; un verdict d'un
    autre lecteur ne prouve pas la mission de celui-ci (R codex3 #1280, B1) ;
    et le verdict que le même lecteur a rendu avant la mission (sa R1, pour
    une mission R2 de delta) ne la prouve pas non plus (R dev4 #1280, B1).
    Sans lecteur, pas de preuve : la mission doit être assignée (ou ses
    relecteurs désignés) avant `done`. Tout l'historique du parent est lu : un
    verdict valide ne se perd pas derrière les cent derniers jalons."""
    if parent is None:
        return False
    mission = item or {}
    readers = set(_mission_readers(mission))
    if not readers:
        return False
    opened = float(mission.get("created_ts") or 0)
    for mark in milestones(db, parent, limit=None):
        if mark.get("kind") != "verdict":
            continue
        if str(mark.get("actor") or "").strip() not in readers:
            continue
        if float(mark.get("at_ts") or 0) >= opened:
            return True
    return False


def _review_parent(item: dict) -> tuple[bool, int | None]:
    """(est-ce un avis après fusion, id du parent relu). L'id est None quand
    la source nomme un avis sans id lisible ; (False, None) sinon. Les avis
    ouverts à la main (`source=mail`, cas #1043/#1218) ne portent que le titre
    `REVUE #<parent> : …`, la convention de `review_lot.title_of` ; les
    missions `R1`/`R2 <harnais> #<parent>` (#1249) sont reconnues par
    `MANUAL_REVIEW_TITLE_RE`. Le titre rattache le parent, il ne le prouve
    pas : la clôture `done` exige en plus le jalon `verdict` (voir `close`)."""
    for field in ("issue_ref", "source"):
        text = str(item.get(field) or "")
        if text.startswith(REVIEW_SOURCE_PREFIX):
            tail = text[len(REVIEW_SOURCE_PREFIX):].strip().lstrip("#").strip()
            return True, int(tail) if tail.isdigit() else None
    title = str(item.get("title") or "")
    match = REVIEW_TITLE_RE.match(title)
    if match:
        return True, int(match.group(1))
    if MANUAL_REVIEW_START_RE.match(title):
        match = MANUAL_REVIEW_TITLE_RE.match(title)
        return True, (int(match.group(1)) if match else None)
    return False, None


def is_review(item: dict) -> bool:
    """Ce lot est-il un avis (relecture après fusion, `REVUE #N`, mission
    `R1`/`R2`) ? Sert à ne pas ouvrir la relecture d'une relecture (garde
    anti-récursion, #1280 B2)."""
    return _review_parent(item)[0]


def get(db: Db, item_id: int) -> dict | None:
    return storage.of(db).work.get(item_id)


def _canonical_state(state: str) -> str:
    """#4353 : l'orthographe persistée d'un état (v1 ou alias générique),
    sinon `WorkError` nommant les orthographes acceptées."""
    from .work_lifecycle import ACCEPTED_STATES, canonical
    try:
        return canonical(state)
    except ValueError:
        raise WorkError("unknown state: %r (%s)"
                        % (state, ", ".join(ACCEPTED_STATES))) from None


def list_items(db: Db, *, state: str | None = None, assignee: str | None = None,
               limit: int = 50) -> list[dict]:
    from .agent_names import resolve as resolve_agent_name
    assignee = resolve_agent_name(assignee, db=db, operation="work.list_items")
    if state and state not in ("merged", "promoted"):
        state = _canonical_state(state)
    return storage.of(db).work.items(state=state, assignee=assignee, limit=limit)


def _pinned_ref(item: dict) -> str:
    """La révision Process qui pinne le lot (colonne 0452), '' sinon.

    Lue dans l'élément rendu par `work.get`, donc portée par l'interface de
    stockage — et par le contrat médié pour une session de VM. Aucun SELECT
    brut ici : il est refusé sur une connexion d'exécuteur médié (B5, #1922).
    Une base d'avant 0452 rend '' (aucun lot n'y est pinné), comme
    `workflow_ref` absent d'un élément.
    """
    return str(item.get("workflow_ref") or "")


def _pinned_transition_error(item_id: int, pinned: str) -> WorkError:
    """Le refus explicite d'un `move` historique sur un lot pinné (#1922)."""
    return WorkError(
        "lot %s is pinned to workflow %s: transitions go through "
        "process_registry" % (item_id, pinned))


def _pinned_close_error(item_id: int, pinned: str) -> WorkError:
    """Le refus explicite d'une fermeture historique sur un lot pinné (#1922)."""
    return WorkError(
        "lot %s is pinned to workflow %s: closing goes through "
        "process_registry" % (item_id, pinned))


def _adopted_ref(db: Db, item_id: int) -> str:
    """La révision Process qui a adopté le lot entre la lecture et l'écriture.

    #1922/N14 : la garde applicative lit `workflow_ref` hors du verrou du lot ;
    le filtre SQL refuse ensuite l'écriture (N8). Relire ici rend le refus
    explicite du lot pinné, au lieu d'un « moved in the meantime » générique
    qui ferait réessayer une écriture réservée au registre.
    """
    return _pinned_ref(get(db, item_id) or {})


def move(db: Db, item_id: int, state: str, *, note: str = "", actor: str = "",
         correct: str | None = None, reviewer: str | None = None,
         package_exclusion: bool = False) -> dict:
    """Déplace un lot en vérifiant la transition, la boucle QA et le terminal.

    `correct` (la raison) : correction d'un état posé par erreur, hors machine
    à états, limitée à `CORRECTIONS` (un `promoted` à tort revient en
    `merged` ; un `merged` à tort, PR d'étape fusionnée trop tôt, revient en
    `build`) ; le journal la dit (« correction : promoted → merged — raison »).
    L'appelant a vérifié qui corrige (`corrector`).

    `reviewer` (#705) : le relecteur désigné, seulement en entrant en `qa` —
    il doit être joignable et différent de l'assigné (`check_reviewer_choice`),
    et un humain, un orchestrateur, un agent de conception ou l'auditeur le
    désigne (`corrector`). La
    désignation est journalisée ; `waiting()`, la vue de progrès et l'alerte
    `stale_lot` le nomment avant tout verdict. Un relecteur du **même harnais
    déclaré** que l'auteur rend un avertissement indicatif (#800), jamais un
    refus : il est posé sur la ligne rendue (`reviewer_warning`). La
    désignation dépose un `request` lié au lot (#2478) qui réveille le
    relecteur ; une remise en échec est un `reviewer_request_warning`.

    `package_exclusion` (#4043) : le lot retiré d'un paquet pour un test
    négatif (`paquet-horaire.exclude`, #4028) repasse de `qa` en `build` sans
    consommer de boucle d'auteur (`loops` inchangé, pas de refus à
    `MAX_QA_LOOPS`) : le retrait est un geste du pilote, pas un renvoi de
    relecture. Seulement qa → build ; l'appelant a vérifié qui exclut
    (`corrector`).

    `state` accepte l'orthographe v1 et le nom générique du cycle de vie
    (#4349 : `in_progress` pour `build`, `to_validate` pour `qa`…)."""
    try:
        state = lifecycle.phase_name(state)
    except ValueError as exc:
        raise WorkError(str(exc)) from None
    if package_exclusion and correct is not None:
        raise WorkError("--package-exclusion and --correct are distinct gestures")
    if package_exclusion and state != lifecycle.IN_PROGRESS:
        raise WorkError("--package-exclusion: a lot excluded from a package returns "
                        "to build (ameesh work move %s build --package-exclusion)" % item_id)
    if state == lifecycle.CLOSED:
        raise WorkError("to close a lot: ameesh work close <id> --abandoned | "
                        "--superseded-by <id> | --delivered --sha <reviewed head> | --done")
    reviewer = (reviewer or "").strip() or None
    reviewer_warning = ""
    if reviewer is not None:
        if state != lifecycle.QA:
            raise WorkError(
                "--reviewer: a reviewer is designated for a review: "
                "ameesh work move %s qa --reviewer %s" % (item_id, reviewer))
        if correct is not None:
            raise WorkError("--reviewer: a correction does not designate a reviewer")
        if not _pg.has_reviewer(db):
            # #705 r2 (N1) : sans la colonne (migration 0421), refuser AVANT
            # d'écrire : sinon le lot entrait en qa sans relecteur, à moitié.
            raise WorkError(
                "--reviewer: work_items.reviewer is missing (migration 0421): "
                "run \"ameesh migrate\", then move the lot to qa with its reviewer")
        if not _pg.has_reviewers(db):
            raise WorkError("--reviewer: work_items.reviewers is missing (migration 0456): "
                            "run \"ameesh migrate\", then move the lot to qa with its reviewers")
        actor = corrector(getattr(db, "cfg", None), db, actor, what="designation",
                          auditor=True)
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    pinned = _pinned_ref(item)
    if pinned:
        # #1922 : un lot pinné sur une révision Process ne bouge que par le
        # registre (process_registry), qui vérifie la garde côté serveur et
        # écrit le reçu dans la même transaction.
        raise _pinned_transition_error(item_id, pinned)
    workflow = _module_registry.development_workflow()
    transitions = workflow.TRANSITIONS
    corrections = workflow.CORRECTIONS
    current = lifecycle.phase(item)
    if current == state:
        raise WorkError("lot %s already in %s" % (item_id, state))
    if reviewer is not None:
        reviewer = check_reviewer_choice(db, item, reviewer,
                                         cfg=getattr(db, "cfg", None))
        # #800 : l'avertissement est calculé sur l'assigné d'avant le move (il
        # ne change pas), et posé sur la ligne rendue quand la désignation
        # aboutit — le CLI l'imprime, les appelants peuvent l'ignorer.
        reviewer_warning = reviewer_family_warning(db, item, reviewer)
    if correct is not None:
        reason = (correct or "").strip()
        if not reason:
            raise WorkError("--correct: the reason for the correction is required")
        if (current, state) not in corrections:
            raise WorkError(
                "correction refused: %s → %s (possible corrections: %s; otherwise the "
                "transitions allowed from %s: %s)"
                % (current, state, ", ".join("%s → %s" % c for c in sorted(corrections)),
                   current, ", ".join(sorted(transitions[current])) or "none"))
        text = "correction: %s → %s — %s%s" % (
            current, state, reason, " (%s)" % note.strip() if note and note.strip() else "")
        row = storage.of(db).work.move(item_id, state, current=current, loops=0, note=text,
                                       actor=actor)
        if row is None:
            # #1922/N14 : adoption concurrente (workflow_ref) ou état changé.
            pinned = _adopted_ref(db, item_id)
            if pinned:
                raise _pinned_transition_error(item_id, pinned)
            raise WorkError("lot %s moved in the meantime: retry" % item_id)
        return row
    if state not in transitions[current]:
        raise WorkError(
            "transition refused: %s → %s (allowed from %s: %s)%s"
            % (current, state, current, ", ".join(sorted(transitions[current])) or "none",
               " — state set by mistake: ameesh work move %s %s --correct \"reason\""
               % (item_id, state) if (current, state) in corrections else ""))
    if state == "validated":
        from . import merge_queue
        gate = merge_queue.review_gate_for_item(
            db, getattr(db, "cfg", None), item, milestones(db, item_id))
        if not gate or not gate.get("ok"):
            raise WorkError(
                "transition to validated requires the review quorum on the current frozen head; "
                "move it to qa")
    if state == lifecycle.WAITING_HUMAN and not storage.of(db).decisions.pending_for_lot(str(item_id)):
        raise WorkError(WAITING_WITHOUT_QUESTION % {"id": item_id})
    if package_exclusion and current not in TO_VALIDATE:
        raise WorkError("--package-exclusion: lot %s is in %s, not in qa: only a "
                        "frozen lot under review is in a package" % (item_id, current))
    # #4043 : un retrait de paquet n'est pas une boucle d'auteur
    returning_loop = current in TO_VALIDATE and state == lifecycle.IN_PROGRESS and not package_exclusion
    if returning_loop and int(item["loops"]) >= MAX_QA_LOOPS:
        raise WorkError(
            "qa → build loop exhausted (%d): move the lot to blocked, or ask the "
            "owner: ameesh decide ask --lot %s --question \"…\""
            % (MAX_QA_LOOPS, item_id))

    held = state == lifecycle.WAITING_HUMAN
    # #1664 r2 (B1) : la note de transition peut poser ou lever une retenue ;
    # elle s'authentifie au même titre que `work note`. Elle se stocke nette,
    # comme `work note` : la lecture groupée ciblée (`lot_hold_events`) lit le
    # motif des retenues sans dépendre des blancs Unicode du texte écrit.
    # #3219 (B2) : le contrôle et la transition se font sous le même verrou de
    # ligne, comme `work note` ; un hold intercalé ne peut pas être effacé.
    note = (note or "").strip()
    text = note or ("%s → %s" % (current, state))
    if reviewer:
        text = "%s — reviewer %s" % (text, reviewer)
    # #5006 : un move par un agent qui n'est plus l'assigné est signalé
    # (journal et sortie), jamais refusé : pilote et relecteur en sont exclus.
    stranger = non_assignee_warning(getattr(db, "cfg", None), db, item, actor)
    if stranger:
        text = "%s\n%s" % (text, stranger)
    from . import planned_waits
    guarded = (planned_waits.is_hold_note(note)
               or planned_waits.is_lift_note(note))
    if guarded and not storage.is_remote(db):
        with db.transaction() as tx:
            locked = storage.of(tx).work.lock(item_id)
            if locked is None:
                raise WorkError("lot %s not found" % item_id)
            actor = check_wait_note(getattr(db, "cfg", None), tx, actor, note,
                                    item_id=item_id)
            row = storage.of(tx).work.move(
                item_id, state, current=current, loops=1 if returning_loop else 0,
                note=text, actor=actor, held=held)
    else:
        actor = check_wait_note(getattr(db, "cfg", None), db, actor, note,
                                item_id=item_id)
        row = storage.of(db).work.move(
            item_id, state, current=current, loops=1 if returning_loop else 0,
            note=text, actor=actor, held=held)
    if row is None:
        if held and not storage.of(db).decisions.pending_for_lot(str(item_id)):
            # the request was answered or withdrawn meanwhile
            raise WorkError(WAITING_WITHOUT_QUESTION % {"id": item_id})
        # #1922/N14 : adoption concurrente (workflow_ref) ou état changé.
        pinned = _adopted_ref(db, item_id)
        if pinned:
            raise _pinned_transition_error(item_id, pinned)
        raise WorkError("lot %s moved in the meantime: retry" % item_id)
    if reviewer:
        # #705 : la désignation est un geste distinct (journalisé), fait dans la
        # même foulée que l'entrée en qa ; le lot y est, l'écriture passe.
        designated = storage.of(db).work.designate_reviewer(item_id, reviewer, actor=actor)
        if designated is None:
            # #705 r2 (N2) : entre le move (écrit) et la désignation, le lot a
            # quitté qa ou l'assigné est devenu le relecteur — ne pas afficher
            # un relecteur jamais posé ; le move, lui, tient.
            raise WorkError(
                "lot %s moved to %s but reviewer %s was not designated (the lot left "
                "qa, or %s now holds it): the move is written; check the lot "
                "(ameesh work show %s) and designate the reviewer once it is in qa"
                % (item_id, state, reviewer, reviewer, item_id))
        row["reviewer"] = designated["reviewer"]
        row["reviewers"] = designated.get("reviewers")
        if reviewer_warning:
            row["reviewer_warning"] = reviewer_warning
        row.update(_reviewer_request(db, item, reviewer, actor))
    if stranger:
        row["assignee_warning"] = stranger
    return row


def _wake_moment(ts: float) -> str:
    """Un instant de réveil, fuseau du poste (lisible dans le journal)."""
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(float(ts)))


#: #2466 : une échéance dans le texte d'une note (`wake_at=2026-…+02:00`),
#: ISO 8601 avec fuseau obligatoire — le même contrat que `--wake-at`.
_WAKE_AT_TOKEN = re.compile(r"(?:^|\s)wake_at=(\S+)", re.IGNORECASE)


def _note_wake_at(text: str) -> float | None:
    match = _WAKE_AT_TOKEN.search(text or "")
    if not match:
        return None
    from . import watch
    try:
        return watch.parse_wake_deadline(None, match.group(1))
    except watch.WatchError as exc:
        raise WorkError("wake_at= in the note: %s" % exc) from exc


def note(db: Db, item_id: int, text: str, *, actor: str = "", once: bool = False,
         wake_ts: float | None = None) -> bool:
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if not (text or "").strip():
        raise WorkError("empty note")
    text = text.strip()
    if text == "-":
        # #3683 : « - » est le marqueur de l'entrée standard, jamais une note.
        raise WorkError("note refused: `-` alone is not a note; write the text, "
                        "or pass `-` to ameesh work note to read it on standard input")
    from . import review_floor
    if review_floor.is_floor_note(text):
        # #3183 : la ligne du plancher d'avis est lue par la porte ; seule la
        # commande du pilote l'écrit, après contrôle de son autorité.
        raise WorkError("note refused: `%s` is reserved to the review floor; use "
                        "ameesh work reviewer %s --floor N" % (review_floor.PREFIX, item_id))
    from . import planned_waits
    note_text = text
    # #3219 (B2) : le détenteur est lu, l'auteur autorisé et la note écrite
    # dans la même transaction, sous le verrou de ligne du lot (le même que
    # `move`, `close_merged` et les autres notes hold/lift). Un hold intercalé
    # entre le contrôle et l'écriture ne peut donc plus être effacé.
    guarded = (planned_waits.is_hold_note(note_text)
               or planned_waits.is_lift_note(note_text))
    cfg = getattr(db, "cfg", None)
    token_ts = _note_wake_at(text)
    if wake_ts is not None and token_ts is not None:
        raise WorkError("give the wake deadline once: --wake-at/--wake-in or "
                        "wake_at= in the note")
    if token_ts is not None:
        wake_ts = token_ts
    locked = not storage.is_remote(db)
    if wake_ts is not None:
        if once:
            raise WorkError("a scheduled wake goes with a regular note, not `once`")
        if item["state"] in lifecycle.COMPLETED:
            raise WorkError("lot %s is %s: no wake to schedule" % (item_id, item["state"]))
        # #2466 : la note porte elle-même son échéance de reprise (témoin
        # lisible dans le journal) ; la colonne `wake_at` la rend exécutoire.
        text = "%s [réveil programmé: %s]" % (text, _wake_moment(float(wake_ts)))
        if guarded and locked:
            with db.transaction() as tx:
                row = storage.of(tx).work.lock(item_id)
                if row is None:
                    raise WorkError("lot %s not found" % item_id)
                if row["state"] in lifecycle.COMPLETED:
                    raise WorkError("lot %s is %s: no wake to schedule"
                                    % (item_id, row["state"]))
                actor = check_wait_note(cfg, tx, actor, note_text, item_id=item_id)
                storage.of(tx).work.note_with_wake(item_id, row["state"], text,
                                                   actor, float(wake_ts))
        else:
            actor = check_wait_note(cfg, db, actor, note_text, item_id=item_id)
            storage.of(db).work.note_with_wake(item_id, item["state"], text, actor,
                                               float(wake_ts))
    else:
        options = {"once": True} if once else {}
        if guarded and locked:
            with db.transaction() as tx:
                row = storage.of(tx).work.lock(item_id)
                if row is None:
                    raise WorkError("lot %s not found" % item_id)
                actor = check_wait_note(cfg, tx, actor, note_text, item_id=item_id)
                storage.of(tx).work.note(item_id, row["state"], text, actor, **options)
        else:
            actor = check_wait_note(cfg, db, actor, note_text, item_id=item_id)
            storage.of(db).work.note(item_id, item["state"], text, actor, **options)
    return True


def schedule_wake(db: Db, item_id: int, wake_ts: float | None, *,
                  text: str = "", actor: str = "") -> dict:
    """#2466 : pose ou efface le réveil programmé d'un lot ouvert.

    Le geste est journalisé (`wake: …`) et ne change pas la retenue d'un lot
    sous `hold:` : le réveil est un rendez-vous explicite, porté par la base
    et consommé par l'exécuteur. `wake_ts=None` annule, en nommant l'échéance
    retirée quand il y en avait une."""
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s not found" % item_id)
    if item["state"] in lifecycle.COMPLETED:
        raise WorkError("lot %s is %s: no wake to schedule" % (item_id, item["state"]))
    why = (text or "").strip()
    previous = item.get("wake_ts")
    if wake_ts is None:
        label = "wake: cancelled"
        if previous:
            label += " (was due %s)" % _wake_moment(float(previous))
        if why:
            label += " — " + why
    else:
        label = "wake: " + (why or "scheduled wake")
        label += " (due %s)" % _wake_moment(float(wake_ts))
    row = storage.of(db).work.schedule_wake(item_id, wake_ts, text=label, actor=actor)
    if row is None:
        raise WorkError("lot %s changed or closed in the meantime: retry" % item_id)
    return row


def events(db: Db, item_id: int, limit: int | None = 100) -> list[dict]:
    return storage.of(db).work.events(item_id, limit)


def events_noted(db: Db, item_id: int, prefix: str,
                 since_ts: float = 0.0) -> list[dict]:
    """Les notes du lot qui commencent par `prefix`, de la plus ancienne à la
    plus récente.

    Contrairement à `events`, la lecture n'est pas une fenêtre des N derniers
    événements : une note ancienne reste lisible sous un flot de notes plus
    récentes (#3279, marque d'armement évincée par les notes `held`)."""
    return storage.of(db).work.events_noted(prefix, float(since_ts),
                                            ids=[int(item_id)])


#: les jalons et la porte du gel/verdict vivent dans `freeze_gate` (#2873),
#: déclaré sensible à deux avis ; `work` les réexporte pour ses appelants.
from .freeze_gate import (  # noqa: E402
    FREEZE_ABSENT_LINE, FREEZE_ABSENT_MARK, FREEZE_UNPUBLISHED_MARK,
    FREEZE_UNVERIFIED_MARK, FROZEN_HEAD_MIN, MANUAL_KINDS, MILESTONE_KINDS,
    REVIEWED_HEAD_MIN, VERDICTS, _frozen_head, freeze_head, frozen_head,
    milestone, milestones, reviewed_head,
)

#: les jalons affichés, du plus récent au plus ancien : (nom, colonne d'instant,
#: colonne de durée) — l'ordre est celui de la frise lue à l'envers. `delivered`
#: (#988) est la fin d'un lot livré hors cible : il remplace la fusion.
MILESTONES = (
    ("delivered", "delivered_ts", None),
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


def promote(db: Db, item_id: int, *, note: str = "", actor: str = "") -> dict:
    """Promote a development delivery using its module's workflow."""
    return _module_registry.development_workflow().promote(
        db, item_id, note=note, actor=actor)


def adopt_workflow(db, item_id: int, *, module: str, spec, actor: str) -> dict:
    """First pin only: lock, authority, activation and initial core, then journal.

    No receipt or revision transfer: subsequent revisions use #4007.
    """
    from . import process_registry as registry
    registry._validate_spec(spec)
    with db.transaction() as tx:
        store = storage.of(tx).work
        row = store.lock(item_id)
        if row is None:
            raise WorkError('pipeline-lot-unknown: %s' % item_id)
        row = store.get(item_id)  # the locked row plus the workflow pin columns
        registry._validate_authority(registry.Authority(actor, source='explicit'))
        registry._check_authority(tx, registry.Authority(actor, source='explicit'))
        if actor != row.get('assignee') and not claims_pilot(db.cfg, tx, actor):
            raise WorkError('pipeline-authority: only the assignee or pilot can adopt')
        if row.get('workflow_ref'):
            raise WorkError('pipeline-already-pinned: use revision transfer')
        phase = lifecycle.phase(row)
        if row['state'] in lifecycle.COMPLETED:
            raise WorkError('pipeline-terminal: completed work item')
        store.lock_workflow_activation(row.get('app') or row.get('workstream') or '')
        if not _module_registry.enabled(tx, row.get('app') or row.get('workstream') or '', module):
            raise WorkError('pipeline-module-inactive: %s' % module)
        if row.get('type') not in spec.types:
            raise WorkError('pipeline-type: work item type is not covered by the card')
        if spec.core_of(spec.initial) != phase:
            raise WorkError('pipeline-stage-mismatch: initial stage must match current core')
        store.adopt_workflow(item_id, ref=spec.workflow_ref,
                             version=spec.workflow_version, stage=spec.initial,
                             state=row['state'], actor=actor)
        return {'work_item_id': int(item_id), 'workflow_ref': spec.workflow_ref,
                'workflow_version': spec.workflow_version, 'workflow_stage': spec.initial}
