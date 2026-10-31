# SPDX-License-Identifier: AGPL-3.0-only
"""Du canon vers le registre (spec §4.4) : `canon sync` et agents éphémères.

`sync` recopie aussi les authentificateurs des fiches Member dans le
registre de confiance (`authenticators`, §8.2), par
`receipts._apply_authenticators` (L6), depuis le canon lu à sa révision
canonique seulement (voir `sync_authenticators`) :

* **sérialisé** (revue L9b, B1) : contrôles et écritures dans UNE
  transaction, sous un verrou consultatif transactionnel du registre pris
  avant tout contrôle (clé indépendante du clone, de l'hôte et du chemin) ;
  sous ce verrou, le dernier commit appliqué (journal `authenticator_syncs`)
  est relu et un commit qui n'en descend pas n'est jamais écrit ;
* **le clone suit le journal** (L814) : le journal est unique par canon, tous
  hôtes confondus ; `sync` rattrape le clone de l'hôte AVANT de le juger —
  fetch quand un dernier commit appliqué est inconnu du dépôt, relecture, puis
  fast-forward du checkout à la révision lue, seulement après les contrôles de
  confiance et de monotonie (une révision non fusionnée refuse sans déplacer le
  checkout). Un checkout qui ne peut pas la contenir (clone divergé,
  modifications locales) fait refuser l'écriture des authentificateurs : jamais
  de registre en avance sur ce que l'hôte a ;
* **la branche canonique ne vient jamais du commit lu** (B2) : configuration
  de l'hôte (`AMEESH_CANON_REF` / `canon_ref`), sinon manifeste du dernier
  commit appliqué, sinon amorçage explicite (`canon sync --bootstrap-ref`,
  journalisé), sinon refus. `--ref` choisit ce qu'on lit, jamais ce qui est
  canonique : le commit lu doit être atteignable depuis la branche de
  confiance (suivi distant `refs/remotes/…`) ; un changement de `ref` dans
  le manifeste ne prend effet qu'une fois fusionné sur la branche actuelle.
  Sans `trusted_ref` explicite, `sync` le prend LUI-MÊME dans la
  configuration de la connexion (`db.cfg.canon_ref`) : aucun appelant
  (exécuteur compris) ne peut l'oublier ;
* **jamais silencieux** (revue codex2) : l'issue est inscrite dans
  `canon_state` (`auth_status` : ok | skipped | error, `auth_diagnostic` ;
  0024) ; une erreur (refus, membre gelé…) y rend la partie
  authentificateurs non-ok et est écrite dans le fil du projet (`fil.record`,
  qui ne lève jamais) — à chaque erreur nouvelle, et au retour à la normale ;
* **écriture gardée** : seul `_sync_locked` écrit le registre, par
  `receipts._apply_authenticators`, qui exige le jeton du verrou et revérifie
  dans `pg_locks` que la session le détient.

Limite connue (§11) : la branche de suivi distant est lue dans le clone de
l'hôte. Un agent qui partage l'utilisateur Unix d'ameesh peut réécrire
`refs/remotes/…` (ou la configuration de l'hôte, ou l'environnement de la
commande) et faire passer un commit non fusionné pour canonique ; seule la
monotonie (journal en base) l'empêche de revenir en arrière. Seule une
vérification par un composant isolé (`ameesh-approve`, autre utilisateur)
est opposable à un agent malveillant.

`sync` met à jour, pour les agents placés sur un hôte, les colonnes
**déclaratives** du registre (harnais, hôte, dossier, modèle, budget,
responsable, équipe, fournisseur, mode d'identifiants, capacités, canon_ref,
placement).
Il ne touche jamais aux colonnes d'**état** (bail, session, statut, dépense,
consigne), avec deux exceptions qui sont les siennes :

* un agent **retiré du canon** (il en venait : `canon_ref` posé, non éphémère)
  passe `stopped` ; s'il est en plein tour (statut `running`, bail vivant),
  sync ne tue rien : il marque seulement l'arrêt demandé dans `status_text`,
  et c'est la synchronisation suivante, une fois le tour fini, qui l'arrête ;
* un agent arrêté par sync puis **réintégré** au canon repart `idle`.

Responsabilité (R14) : la colonne `responsible` ne reçoit que le responsable
**résolu** (fiche Member humaine unique) d'un agent sans erreur bloquante.
Sinon elle est vidée : l'agent n'est plus réclamable quand le responsable est
requis (`registry.claimable`). Une erreur sans sujet (fédération, fiche du
profil illisible…) bloque tous les agents de l'hôte : un canon invalide
empêche les nouvelles réclamations sans arrêter les agents déjà réclamés.

État du canon (§4.1, fail closed) : chaque `sync` enregistre dans
`canon_state` le statut du canon pour l'hôte — `ok`, `invalid` (erreur qui
bloque tout l'hôte) ou `unreadable` (racine absente, dépôt cassé, révision
introuvable) — avec un diagnostic. Un canon illisible ne laisse au registre
que cette ligne (aucune autre écriture). La condition de réclamation
(`registry.canon_claim_predicate_sql`) refuse les agents du canon, et leurs
éphémères, tant que cet état n'est pas `ok` ; baux, sessions et tours en
cours ne sont pas touchés.

Placement gouverné (C4, R18 ; 0022) : pour chaque agent du canon de l'hôte,
sync écrit l'admissibilité de son placement sur CET hôte (`placement_ok`,
`placement_diagnostic`, `placement_ref` ; voir `placement.evaluate`) : admis
par la politique de la fiche Host, ou refusé — politique violée, aucun
placement sur cet hôte, placement ambigu, fiche invérifiable. Chaque verdict
est écrit avec le PROFIL ÉVALUÉ (`placement_profile`, calculé par la
fonction SQL `ameesh_placement_profile` sur les valeurs jugées : hôte,
harnais, fournisseur, modèle, mode d'identifiants). La condition de
réclamation exige `placement_ok` pour un agent gouverné par le canon, et
que son profil courant soit le profil évalué : toute divergence posée
ensuite (inscription, import, SQL à la main) le ferme jusqu'au sync suivant.
Un éphémère hérite du verdict de son créateur racine du canon (recopié par
`spawn`, rafraîchi par sync) seulement si SON profil est le profil évalué de
ce créateur ; sinon il est refusé, comme quand ce créateur n'est plus sur
cet hôte ou que la lignée est rompue. sync ne place ni ne déplace un agent
de lui-même : il suit les fiches Placement du canon.

Plan de travail (L29) : `sync` recopie aussi les fiches `WorkPackage` dans
`work_packages` (`sync_packages`), avec leur `canon_ref`, quel que soit
l'hôte (le plan est commun). Une fiche en erreur n'est pas écrite (sa copie
précédente reste) ; une fiche absente d'un membre lu est marquée retirée,
jamais effacée ; les lots rattachés suivent le parent courant de leur fiche.
Une erreur du plan ne bloque aucun agent.

Plusieurs canons (L42, décision 0031) : un hôte lit une liste de canons, le
premier étant le canon PAR DÉFAUT. `sync(db, canon, host)` ne lit, n'arrête,
n'efface et ne retire que les lignes de CE canon — colonne `canon` du
registre et des paquets, NULL = canon par défaut (lignes historiques et
lignes qu'il écrit) ; un autre canon y écrit son identifiant. L'état est
tenu par (hôte, canon) (`canon_state.canon` : '' pour le canon par défaut).
Les noms restent globaux : un canon qui déclare un agent (ou un paquet) dont
la ligne appartient à un autre canon et n'est pas LIBÉRÉE (arrêtée par sync
parce que retirée de son canon) reçoit un constat `canon-name-conflict`
limité à cette fiche, sans aucune écriture ; un nom libéré est repris (la
ligne change de canon et repart.

Authentificateurs par canon (L44, décision 0031, migration 0035) : CHAQUE
canon synchronise ses authentificateurs, dans SON registre (colonne
`authenticators.canon`, '' = canon par défaut, comme `canon_state.canon`) et
avec SON journal (`authenticator_syncs.canon`) :

* le dernier commit appliqué, la monotonie (descendance des commits) et la
  branche « applied » se lisent dans le journal de ce canon — synchroniser A,
  puis B, puis A ne compare jamais un commit de B à un commit de A ;
* la branche de CONFIANCE est celle de la configuration de ce canon : `ref`
  de son entrée `canons` (`canon_ref` / AMEESH_CANON_REF pour le canon par
  défaut ; voir `configured_ref`), sinon le manifeste du dernier commit
  appliqué de ce canon, sinon `--bootstrap-ref`, sinon refus ; un canon lu
  hors git (`untrusted`) ne l'écrit pas ;
* « absent du canon » veut dire absent de CE canon : la révocation ne vise que
  les lignes de ce canon ; un humain déclaré dans deux canons y a des lignes
  distinctes, révoquées chacune par son seul canon ;
* l'issue est inscrite dans l'état de ce canon (`canon_state` de la clé du
  canon) : une erreur de l'un n'affecte ni le registre ni l'état de l'autre.
  Le verrou du registre reste unique (les synchronisations se sérialisent).

Le SQL est dans le stockage (`storage.of(db).canon`, `.ephemerals`,
`.authenticators`, spec §10) ; la transaction du registre des
authentificateurs, verrou compris, y est UNE opération
(`authenticators.under_registry_lock`).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field

from . import canon as canon_mod
from . import fil
from . import harnesses
from . import mechanics
from . import placement as placement_mod
from . import receipts, registry, storage
from . import visibility as visibility_mod
from .canon import Canon, Finding
from .config import NAME_RE
from . import db as db_mod
from .db import Db, DbError

#: préfixes de `status_text` posés par sync (et reconnus par lui seul)
STOP_MARK = "retiré du canon"
PENDING_MARK = "arrêt demandé en fin de tour : retiré du canon"
REVIVED_TEXT = "réintégré au canon"

MAX_EPHEMERAL_TTL = 7 * 24 * 3600.0
_TTL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.I)

DECLARATIVE = ("harness", "host", "cwd", "model", "budget_usd", "responsible", "team",
               "provider", "credential_mode", "capabilities", "canon_ref", "ephemeral",
               "priority", "admitted_hosts", "admitted_tags", "memory_repository",
               "visibility_ok", "visibility_diagnostic",
               "placement_ok", "placement_diagnostic", "placement_ref", "projects", "aliases",
               # L244 (#420) : les rôles déclarés d'une fiche Agent/Persona,
               # persistés pour le garde SQL du rôle QA. Le vocabulaire est
               # celui des fiches (L242c, #414) ; aucune criticité n'est écrite.
               "roles",
               # L165 (EP1) : relève (`hote`/`election`) et hôtes candidats
               "releve", "candidats",
               # #3185 : projets relus hors équipe, lus par la désignation
               "relit")

#: statuts de `canon_state` (0008)
CANON_OK = "ok"
CANON_INVALID = "invalid"
CANON_UNREADABLE = "unreadable"
SYNC_INCOMPLETE = "canon-sync-incomplete"
_DIAGNOSTIC_MAX = 4000
#: #3955 : tentatives d'écriture conditionnelle du commit audité quand une
#: synchronisation concurrente a avancé la ligne entre la lecture et l'écriture.
#: Au-delà, on ne réessaie plus : l'état garde le commit le plus récent.
_COMMIT_CAS_ATTEMPTS = 5


class SpawnError(ValueError):
    pass


class CanonUnreadable(canon_mod.CanonError):
    """Canon illisible : l'état `unreadable` est enregistré, rien d'autre."""

    def __init__(self, host: str, diagnostic: str, findings: list[Finding]):
        super().__init__("unreadable canon, registry unchanged: %s" % diagnostic)
        self.host = host
        self.diagnostic = diagnostic
        self.findings = findings


class CanonStale(canon_mod.CanonError):
    """Processus plus vieux que le paquet installé (L780) : rien n'est écrit.

    Un exécuteur non redémarré après une livraison porte l'ancien code — dont
    l'ancien validateur de descripteurs — alors que le paquet sur disque a
    changé. Ses constats ne valent pas pour le paquet en place : `sync`
    s'arrête AVANT toute écriture, `canon_state` reste celui du dernier passage
    valide (les responsables et les placements aussi), et l'appelant demande un
    redémarrage entre deux tours.
    """

    def __init__(self, host: str, reason: str):
        super().__init__("outdated code, canon unchanged: %s" % reason)
        self.host = host
        self.reason = reason


@dataclass
class SyncAction:
    agent: str
    action: str     # créé | mis à jour | inchangé | arrêté | arrêt demandé | réintégré | déplacé | laissé | différé
    detail: str = ""
    blocked: list[str] = field(default_factory=list)
    #: verdict de placement écrit pour cet agent (C4), s'il en a un
    placement: placement_mod.Verdict | None = None

    def to_dict(self) -> dict:
        out = {"agent": self.agent, "action": self.action, "detail": self.detail}
        if self.blocked:
            out["blocked"] = self.blocked
        if self.placement is not None:
            out["placement_ok"] = self.placement.ok
            out["placement_diagnostic"] = self.placement.diagnostic
            out["placement_ref"] = self.placement.ref
        return out


#: statuts de la synchronisation du registre des authentificateurs
AUTH_SYNCED = "synced"       # receipts._apply_authenticators appelé
AUTH_SKIPPED = "skipped"     # rien écrit, sans erreur (canon non approuvé : tests)
AUTH_REFUSED = "refused"     # rien écrit, ERREUR (révision hors branche, canon en retard,
                             # non descendant, premier amorçage sans --bootstrap-ref)

#: partie authentificateurs de l'état du canon (`canon_state.auth_status`, 0024)
AUTH_STATE_OK = "ok"         # synchronisé sans erreur
AUTH_STATE_SKIPPED = AUTH_SKIPPED   # rien écrit, sans erreur
AUTH_STATE_ERROR = "error"   # refus, ou erreurs (membre gelé, entrée illisible…) : non-ok
#: auteur des entrées du fil écrites par sync
FIL_SENDER = "ameesh:canon-sync"
#: #3741 : en tête du diagnostic durable d'une erreur PASSAGÈRE de la base
#: (délai de requête, verrou, connexion) qui n'a pas été annoncée au fil
QUIET_MARK = "[transient database error, not announced] "
#: SQLSTATE d'une erreur passagère : requête annulée (dont statement_timeout),
#: verrou indisponible, interblocage, sérialisation ; et la classe 08
#: (connexion)
_TRANSIENT_SQLSTATES = frozenset((db_mod.QUERY_CANCELED, "55P03",
                                  db_mod.DEADLOCK_DETECTED, "40001"))


def transient_db_error(exc: BaseException) -> bool:
    """#3741 : l'erreur de base passera-t-elle d'elle-même au prochain sync ?
    Classée par son SQLSTATE ou son type, jamais par son texte."""
    if isinstance(exc, (db_mod.Unavailable, receipts.RegistryLockError)):
        return True
    state = getattr(exc, "sqlstate", None) or ""
    return state in _TRANSIENT_SQLSTATES or state.startswith("08")


@dataclass
class AuthenticatorSync:
    """Canon → registre des authentificateurs (§8.2) : ce qui a été fait."""

    status: str
    reason: str = ""
    #: résultat de `receipts._apply_authenticators` (statut `synced`)
    result: dict | None = None
    #: canon lu en partie : aucune révocation pour absence (raisons)
    partial: list[str] = field(default_factory=list)
    #: origine de la branche canonique de confiance (config | applied | bootstrap)
    trust: str = ""
    #: branche canonique de confiance (suivi distant, ex. origin/main)
    branch: str = ""
    #: remarques (amorçage journalisé, --bootstrap-ref ignoré…)
    notes: list[str] = field(default_factory=list)
    #: L44 (0031) : canon du registre ('' = canon par défaut, sinon son id)
    canon: str = ""
    #: #3741 : refus dû à une erreur passagère de la base (`transient_db_error`)
    transient: bool = False

    @property
    def errors(self) -> list[str]:
        if self.status == AUTH_REFUSED:
            return [self.reason]
        return list((self.result or {}).get("errors") or [])

    @property
    def state(self) -> str:
        """Partie authentificateurs de l'état du canon : ok | skipped | error."""
        if self.errors:
            return AUTH_STATE_ERROR
        return AUTH_STATE_SKIPPED if self.status == AUTH_SKIPPED else AUTH_STATE_OK

    def diagnostic(self) -> str:
        """Diagnostic durable (`canon_state.auth_diagnostic`), borné."""
        if self.status == AUTH_REFUSED:
            text = "REFUSED, registry unchanged — %s" % self.reason
        elif self.errors:
            frozen = list((self.result or {}).get("frozen") or [])
            text = "%d error(s)%s: %s" % (
                len(self.errors), " (FROZEN: %s)" % ", ".join(frozen) if frozen else "",
                "; ".join(self.errors))
        elif self.status == AUTH_SKIPPED:
            text = self.reason
        else:
            text = "; ".join(list(self.notes) + (
                ["canon partially read, no revocation for absence: %s"
                 % ", ".join(self.partial)] if self.partial else []))
        return _clip(text)

    def to_dict(self) -> dict:
        out: dict = {"status": self.status, "reason": self.reason,
                     "errors": self.errors, "partial": self.partial,
                     "trust": self.trust, "branch": self.branch, "notes": self.notes,
                     "state": self.state, "canon": self.canon}
        for key in ("added", "updated", "revoked", "frozen"):
            out[key] = list((self.result or {}).get(key) or [])
        out["unchanged"] = int((self.result or {}).get("unchanged") or 0)
        return out


@dataclass
class SyncReport:
    host: str
    source: str
    untrusted: bool
    actions: list[SyncAction]
    findings: list[Finding]
    status: str = CANON_OK          # état enregistré dans canon_state
    diagnostic: str = ""
    #: registre des authentificateurs (§8.2), synchronisé par le même passage
    authenticators: AuthenticatorSync | None = None
    #: plan de travail (L29) : fiches WorkPackage recopiées
    packages: "PackageSync | None" = None
    #: #538 : règles de travail (fiches Process) poussées aux agents de l'hôte
    processes: "ProcessSync | None" = None
    #: #1616 : paramètres mécaniques compilés du canon appliqué (constat)
    mechanics: "MechanicsSync | None" = None
    shared: bool = False             # #3963: reused periodic host result

    @property
    def errors(self) -> list[Finding]:
        return canon_mod.errors(self.findings)

    @property
    def auth_status(self) -> str:
        """Partie authentificateurs de l'état (`canon_state.auth_status`) ; non-ok
        (`error`) dès qu'une erreur d'authentificateurs est constatée."""
        return self.authenticators.state if self.authenticators is not None else ""

    def to_dict(self) -> dict:
        return {"host": self.host, "source": self.source, "untrusted": self.untrusted,
                "canon_status": self.status, "diagnostic": self.diagnostic,
                "auth_status": self.auth_status,
                "auth_diagnostic": (self.authenticators.diagnostic()
                                    if self.authenticators is not None else ""),
                "actions": [a.to_dict() for a in self.actions],
                "errors": len(self.errors),
                "findings": [f.to_dict() for f in self.findings],
                "authenticators": (self.authenticators.to_dict()
                                   if self.authenticators is not None else None),
                "packages": self.packages.to_dict() if self.packages is not None else None,
                "processes": (self.processes.to_dict()
                              if self.processes is not None else None),
                "mechanics": (self.mechanics.to_dict()
                              if self.mechanics is not None else None)}


# --------------------------------------------------------------------------
# état du canon par hôte (canon_state, §4.1)
# --------------------------------------------------------------------------

def _clip(text: str) -> str:
    return text if len(text) <= _DIAGNOSTIC_MAX else text[:_DIAGNOSTIC_MAX - 1] + "…"


def _describe(findings: list[Finding]) -> str:
    return _clip("; ".join("%s (%s): %s" % (f.code, f.where(), f.message) for f in findings))


def assess(canon: Canon, host: str,
           findings: list[Finding] | None = None) -> tuple[str, str]:
    """(statut, diagnostic) du canon pour `host`, tel que `sync` l'enregistre.

    `unreadable` : le canon n'a pas pu être lu (aucune donnée utilisable).
    `invalid` : lu, mais une erreur bloque tous les agents de l'hôte (erreur
    sans sujet, ou liée à la fiche Host). `ok` sinon ; une erreur propre à un
    agent ne bloque que lui (son `responsible` est vidé) et figure au
    diagnostic.
    """
    if not canon.readable:
        reasons = canon_mod.errors(canon.load_findings)
        return CANON_UNREADABLE, _describe(reasons) if reasons else "no source read"
    if findings is None:
        findings = canon_mod.validate(canon)
    block = canon_mod.blocking(findings)
    host_wide = block.global_errors + list(block.by_host.get(host, []))
    if host_wide:
        return CANON_INVALID, _describe(host_wide)
    others = canon_mod.errors(findings)
    if others:
        return CANON_OK, ("errors specific to agents (not claimable) or to the plan: "
                          + _describe(others))
    return CANON_OK, ""


def known_projects(db: Db, cfg=None) -> set[str]:
    """L243 (#435) : les projets connus hors du canon — `github.projects` de
    l'hôte et les projets des lots (`agent-project-unknown`).

    #495: a given `cfg` wins over `db.cfg`; with neither, no declared project."""
    from . import plan_github
    try:
        declared = set(plan_github.settings_of(
            cfg if cfg is not None else getattr(db, "cfg", None)).projects)
    except plan_github.SettingsError:
        declared = set()
    return declared | set(storage.of(db).canon.lot_projects())


def state_key(db: Db, canon: Canon, host: str) -> str:
    """Clé de `canon_state.canon` pour ce canon sur `host` (L42, 0031).

    '' pour le canon par défaut ; son identifiant sinon. Un canon non par
    défaut dont l'identifiant ne vient pas de sa fédération (illisible,
    federation.yaml absent ou invalide) garde la clé déjà enregistrée pour sa
    racine : un canon devenu illisible ferme bien SES agents (fail closed)."""
    if canon.is_default:
        return ""
    if canon.id_source != canon_mod.ID_FROM_FEDERATION:
        try:
            known = storage.of(db).canon.key_for_root(host, canon.root)
        except DbError:
            known = None
        if known:
            return known
    return canon.id


def audited_commit(db: Db, cfg, canon: Canon, source) -> str:
    """Le commit exact à lire pour ce canon : le dernier audité de l'hôte.

    `canon_state.last_good_commit` est le commit que l'hôte a audité ; la
    référence configurée peut être en avance (un fetch sans synchro). Un état
    enregistré fait foi même si le passage courant est invalide (B2 de la
    relecture cx2 de #2447) : sans dernier commit valide il n'y a pas de
    politique auditée, et l'on ne retombe jamais sur la référence. Sans état
    du tout (banc, premier démarrage), le commit exact de la source est le
    seul connu ; `mechanics.load` refuse encore une référence non commit.

    Une **lecture impossible** de l'état n'est pas une absence d'état : elle
    remonte à l'appelant, qui refuse la politique ou les familles sans jamais
    lire la référence configurée, non auditée.
    """
    key = state_key(db, canon, getattr(cfg, "host", ""))
    state = storage.of(db).canon.state(getattr(cfg, "host", ""), key)
    if state is not None:
        commit = str(state.get("last_good_commit") or "").strip().lower()
        root = str(state.get("root") or "")
        if commit and (not root or root == str(getattr(canon, "root", "") or "")):
            return commit
        return ""
    return str(getattr(source, "commit", "") or "").strip().lower()


def _commit_relation(canon: Canon | None, old: str, new: str) -> str:
    """Rapport d'ancienneté entre le commit enregistré `old` et `new`.

    ``forward`` (aucun enregistré, même commit, ou `new` descend de `old`),
    ``ancestor`` (`new` est un ancêtre : retour arrière), ``unknown`` (le
    commit enregistré n'est pas dans ce dépôt — clone en retard) ou
    ``unrelated`` (histoires distinctes / canon réécrit). Seul ``forward``
    autorise l'avance sans retour arrière déclaré : dans le doute on ne
    touche pas au commit audité. #3955 — la synchro qui a lu la référence
    avant un fetch et écrit après ne fait plus reculer la porte."""
    if not old or old == new:
        return "forward"
    source = canon.sources[0] if canon is not None and canon.sources else None
    repo = str(getattr(source, "repo", "") or "")
    if not repo or str(getattr(source, "mode", "")) != "git":
        return "unrelated"
    if canon_mod.git_contains(repo, old, new):
        return "forward"
    if canon_mod.git_contains(repo, new, old):
        return "ancestor"
    try:
        if canon_mod.git_commit(repo, old) is None:
            return "unknown"
    except canon_mod.GitError:
        return "unknown"        # git illisible : le doute ne fait jamais avancer
    return "unrelated"


def _journal_rewind(db: Db, host: str, canon: Canon | None, key: str,
                    old: str, new: str) -> None:
    """#3955 : journalise un retour arrière DÉCLARÉ du commit audité (thread)."""
    relation = _commit_relation(canon, old, new)
    which = " (canon %s)" % key if key else ""
    text = ("canon sync on %s%s: DECLARED REWIND of the audited commit — "
            "last_good_commit %s -> %s (%s). The train and the merge gate now "
            "read the older commit; explicit `ameesh canon sync --allow-rewind`."
            % (host, which, old[:12], new[:12], relation))
    cfg = getattr(db, "cfg", None)
    if cfg is None:
        fil.warn(text)
        return
    try:
        fil.record(cfg, db, sender=FIL_SENDER, recipients=["all"], text=text,
                   project=fil.project_for(cfg),
                   meta={"event": "canon-rewind", "host": host, "canon": key,
                         "from": old, "to": new, "relation": relation})
    except Exception as exc:  # noqa: BLE001 - fil.record ne lève pas ; prudence
        fil.warn("%s (thread entry not written: %s)"
                 % (text, " ".join(str(exc).split())[:200]))


def record_state(db: Db, host: str, status: str, diagnostic: str,
                 canon: Canon | None = None, *, key: str | None = None,
                 allow_rewind: bool = False) -> None:
    """Écrit l'état du canon de `host` ; le dernier commit valide ne suit que `ok`.

    L42 (0031) : `key` ('' = canon par défaut, sinon l'identifiant du canon) ;
    sans `key`, celle du canon (`state_key`), ou '' sans canon.

    #3955 : le commit audité ne recule JAMAIS vers un ancêtre (ni vers une
    histoire que ce dépôt ne peut pas situer) sans retour arrière DÉCLARÉ
    (`allow_rewind`, `canon sync --allow-rewind`) ; ce retour-là est journalisé.
    L'écriture est conditionnelle (`expected`, CAS) puis rejouée : une synchro
    concurrente qui enregistre un commit plus récent entre notre lecture et
    notre écriture n'est jamais écrasée par une plus ancienne."""
    commit = None
    if status == CANON_OK and canon is not None and canon.sources:
        commit = canon.sources[0].commit or None
    if key is None:
        key = state_key(db, canon, host) if canon is not None else ""
    canon_id = None
    if canon is not None and canon.id_source == canon_mod.ID_FROM_FEDERATION:
        canon_id = canon.id
    elif key:
        canon_id = key
    store = storage.of(db).canon

    def write(expected: str | None) -> str | None:
        return store.record_state(
            host, status, root=canon.root if canon is not None else "",
            source=canon.source_label() if canon is not None and canon.sources else "",
            commit=commit, good=status == CANON_OK, diagnostic=diagnostic,
            canon=key, canon_id=canon_id, expected=expected)

    if status != CANON_OK or not commit:
        write(None)
        return
    if allow_rewind:
        previous = store.state(host, key) or {}
        old = str(previous.get("last_good_commit") or "")
        written = write(None)
        if written == commit and old and old != commit:
            _journal_rewind(db, host, canon, key, old, commit)
        return
    for _ in range(_COMMIT_CAS_ATTEMPTS):
        previous = store.state(host, key) or {}
        old = str(previous.get("last_good_commit") or "")
        if _commit_relation(canon, old, commit) != "forward":
            # Un sync en retard (référence lue avant le fetch) garde l'état
            # enregistré : ni le commit, ni le statut, ni la source ne reculent.
            return
        if write(old) == commit:
            return
        # CAS perdu : une synchro concurrente a avancé la ligne ; on relit et
        # on décide sur le commit qu'elle a enregistré.
    # Épuisement (course très disputée) : ne jamais écrire l'ancien commit.


def state(db: Db, host: str, canon: "Canon | str | None" = None) -> dict | None:
    """Dernier état enregistré d'un canon sur `host` ('' / None : le canon
    par défaut ; un Canon : sa clé), ou None."""
    if isinstance(canon, Canon):
        key = state_key(db, canon, host)
    else:
        key = canon or ""
    return storage.of(db).canon.state(host, key)


def states(db: Db, host: str) -> list[dict]:
    """Tous les états enregistrés pour `host`, un par canon (L42)."""
    return storage.of(db).canon.states(host)


class DefaultCanonError(canon_mod.CanonError):
    """L46 : changement de canon par défaut refusé (identité de l'ancien ou du
    nouveau canon par défaut inconnue). Rien n'est rebasé ; l'appelant ne
    synchronise AUCUN canon tant que l'erreur n'est pas levée."""


def _same_root(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return os.path.realpath(left) == os.path.realpath(right)


def adopt_default(db: Db, host: str, canons: list[Canon]) -> dict | None:
    """Changement de canon par défaut sur `host` (L42, 0031).

    Les lignes sans canon (NULL) appartiennent au canon par défaut. Si l'état
    du canon par défaut de l'hôte a été écrit par un AUTRE canon (son
    `canon_id`), et que cet ancien canon par défaut est encore configuré
    après le premier, ses lignes NULL reçoivent son identifiant AVANT toute
    synchronisation : le nouveau canon par défaut ne les prend pas pour les
    siennes (il les arrêterait). Rien n'est arrêté ni effacé. Rend ce qui a
    été rebasé, ou None.

    L46 : tout se décide et s'écrit dans UNE transaction, sous le verrou du
    registre des authentificateurs puis celui de la ligne '' de `canon_state`
    (relue `FOR UPDATE`) : le nouveau `canon_id` est écrit avec le rebase. Une
    `canon sync` concurrente, ou une exception après le rebase (transaction
    annulée), ne le rejoue donc jamais (B réétiqueté en A). Lève
    `DefaultCanonError` — et marque la ligne '' `invalid`, ce qui ferme les
    agents du canon par défaut sans rien arrêter — quand l'ordre des canons
    change alors que l'ancien canon par défaut n'a pas d'identifiant
    enregistré (ligne antérieure à L42), ou que le nouveau n'a pas
    d'identifiant de fédération : ses lignes NULL seraient sinon données à
    l'autre canon."""
    if len(canons) < 2:
        return None
    new = canons[0]
    # régime établi (lecture sans verrou) : le canon par défaut enregistré est
    # déjà le premier configuré — rien à décider, ni verrou ni transaction.
    # Tout autre cas est relu et décidé SOUS verrou ci-dessous.
    seen = storage.of(db).canon.state(host, "")
    if seen is None or (seen.get("canon_id") and seen.get("canon_id") == new.id):
        return None
    others = {c.id for c in canons[1:] if c.id_source == canon_mod.ID_FROM_FEDERATION}
    refus = None
    with db.transaction() as tx:
        # L44 (0031) : le registre des authentificateurs et son journal suivent
        # leur canon, sous le verrou du registre (pris d'abord) : ceux du canon
        # '' passent à l'ancien canon par défaut, ceux du nouveau à ''. Sans
        # cela, les passkeys de l'ancien canon par défaut vaudraient pour les
        # objets du nouveau.
        store = storage.of(tx).authenticators
        store.lock_registry()
        canon_store = storage.of(tx).canon
        current = canon_store.lock_default_state(host)
        if current is None:
            return None
        previous = current.get("canon_id")
        if not previous:
            if _same_root(current.get("root"), new.root):
                # même canon par défaut qu'avant : son identifiant est inscrit
                # maintenant (la synchronisation l'aurait fait)
                if new.id_source == canon_mod.ID_FROM_FEDERATION:
                    canon_store.set_default_id(host, new.id)
                return None
            if not current.get("root"):
                return None  # aucun canon n'a encore écrit la ligne : rien à protéger
            refus = ("default canon of %s: the former default canon (root %s) has "
                     "no recorded identifier, and the first configured canon (%s, %s) "
                     "is not that one — order change refused, otherwise its rows "
                     "would be given to the other canon. First sync with the former "
                     "default canon alone or first (its identifier is then "
                     "recorded), then change the order."
                     % (host, current.get("root"), new.id, new.root))
        elif previous == new.id or previous not in others:
            return None
        elif new.id_source != canon_mod.ID_FROM_FEDERATION:
            refus = ("default canon of %s: %s (root %s) would replace %s without a "
                     "federation identifier (federation.yaml missing or unreadable) — "
                     "order change refused: fix its federation.yaml."
                     % (host, new.id, new.root, previous))
        if refus is None:
            moved = store.rebase_default(previous, new.id)
            done = canon_store.rebase_default(host, previous, new.id)
            # L46 : écrit avec le rebase, dans la même transaction
            canon_store.set_default_id(host, new.id)
        else:
            canon_store.flag_default(host, CANON_INVALID, refus)
    if refus is not None:
        fil.warn(refus)
        raise DefaultCanonError(refus)
    done["authenticators"] = moved
    fil.warn("default canon of %s: %s replaces %s — %d agent(s) and %d package(s) "
             "attached to %s; authenticators: %d attached to %s, %d to the default "
             "canon" % (host, new.id, previous, len(done["agents"]),
                         len(done["packages"]), previous,
                         moved["authenticators"][0], previous, moved["authenticators"][1]))
    return done


def close_unconfigured(db: Db, host: str, canons: list[Canon]) -> list[str]:
    """Ferme (état `invalid`) les canons de `host` qui ne sont plus configurés
    (L42, 0031) : leurs agents ne sont plus réclamables, sans rien arrêter.
    `canons` : TOUS les canons configurés, chargés (l'appelant ne l'appelle
    pas si l'un d'eux n'a pas pu l'être). Rend les clés fermées."""
    keep = [state_key(db, canon, host) for canon in canons]
    closed = storage.of(db).canon.close_others(
        host, keep, "canon removed from the host configuration: agents not claimable "
                    "(to stop them: agent-runner stop)")
    if closed:
        fil.warn("canon(s) removed from the configuration of %s: %s — agents not "
                 "claimable" % (host, ", ".join(c or "(default)" for c in closed)))
    return closed


def record_authenticators(db: Db, host: str, done: AuthenticatorSync,
                          key: str = "") -> None:
    """Inscrit l'issue de la synchronisation du registre des authentificateurs.

    Diagnostic durable : `canon_state.auth_status` / `auth_diagnostic` de
    `host` (la ligne de l'hôte, écrite juste avant par `record_state`). Une
    erreur rend cette partie non-ok (`error`) sans toucher au statut du canon
    pour les agents (`status`, réclamation). Fil : une erreur NOUVELLE (statut
    ou diagnostic différent du précédent) est écrite dans le fil du projet,
    et le retour à la normale aussi — un exécuteur qui resynchronise
    périodiquement ne répète pas la même entrée, mais rien n'est silencieux.

    #3741 : une erreur PASSAGÈRE de la base (`done.transient` : délai de
    requête, verrou, connexion) qui suit un état sans erreur n'est pas
    annoncée — sous charge, chaque délai dépassé écrivait deux diffusions à
    « all » (l'erreur, puis le retour à la normale au sync suivant), dix fois
    en une matinée. Elle reste inscrite dans l'état durable, marquée
    `QUIET_MARK` ; si le sync suivant échoue encore, l'erreur est annoncée
    (le diagnostic marqué diffère), et le retour à la normale n'est annoncé
    que si une erreur l'a été.
    """
    status, diagnostic = done.state, done.diagnostic()
    quiet = status == AUTH_STATE_ERROR and done.transient
    st = storage.of(db).canon
    previous = st.record_auth_state(
        host, status, diagnostic, key, quiet_prefix=QUIET_MARK if quiet else "")
    before = (previous.get("auth_status"), previous.get("auth_diagnostic") or "") \
        if previous is not None else (None, "")
    was_quiet = before[0] == AUTH_STATE_ERROR and before[1].startswith(QUIET_MARK)
    if quiet:
        if before[0] != AUTH_STATE_ERROR:
            return
        # The storage transition already promoted a repeated transient error
        # under the same lock; never overwrite another executor's recovery.
    # L44 (0031) : chaque canon a son registre ; le fil dit lequel
    which = " (canon %s)" % key if key else ""
    option = " --canon %s" % key if key else ""
    if status == AUTH_STATE_ERROR and before != (status, diagnostic):
        _post_authenticators(db, host, status, (
            "canon sync on %s%s: authenticator registry IN ERROR — %s. "
            "Details: ameesh canon sync --host %s%s; durable state: ameesh canon check "
            "--host %s%s." % (host, which, diagnostic or "?", host, option, host, option)),
            done)
    elif status != AUTH_STATE_ERROR and before[0] == AUTH_STATE_ERROR and not was_quiet:
        _post_authenticators(db, host, status, (
            "canon sync on %s%s: authenticator registry error-free again "
            "(%s)%s." % (host, which, status, " — %s" % diagnostic if diagnostic else "")),
            done)


def _post_authenticators(db: Db, host: str, status: str, text: str,
                         done: AuthenticatorSync) -> None:
    """Entrée du fil du projet (`fil.record`, qui ne lève jamais) ; une
    connexion sans configuration se dit sur stderr."""
    cfg = getattr(db, "cfg", None)
    if cfg is None:
        fil.warn(text)
        return
    try:
        fil.record(cfg, db, sender=FIL_SENDER, recipients=["all"], text=text,
                   project=fil.project_for(cfg),
                   meta={"event": "authenticators_%s" % status, "host": host,
                         "auth_status": status, "auth": done.status,
                         "branch": done.branch, "trust": done.trust,
                         "canon": done.canon})
    except Exception as exc:  # noqa: BLE001 - fil.record ne lève pas ; prudence
        fil.warn("%s (thread entry not written: %s)" % (text, " ".join(str(exc).split())[:200]))


# --------------------------------------------------------------------------
# registre des authentificateurs (§8.2, L6)
# --------------------------------------------------------------------------

#: Le verrou consultatif TRANSACTIONNEL du registre (B1) est défini une seule
#: fois, avec l'écriture qu'il garde : `receipts.lock_registry` (clé du seul
#: registre, ni du clone ni de l'hôte), `receipts._apply_authenticators`.

#: d'où vient la branche canonique de CONFIANCE (journal `authenticator_syncs`)
TRUST_CONFIG = "config"          # configuration de l'hôte (AMEESH_CANON_REF / canon_ref)
TRUST_APPLIED = "applied"        # manifeste du dernier commit déjà appliqué
TRUST_BOOTSTRAP = "bootstrap"    # premier amorçage : --bootstrap-ref explicite
TRUST_LABELS = {
    TRUST_CONFIG: "host configuration",
    TRUST_APPLIED: "manifest of the last applied commit",
    TRUST_BOOTSTRAP: "--bootstrap-ref bootstrap",
}
BOOTSTRAP_REFUSED = (
    "first bootstrap of the authenticator registry refused: no trusted canonical "
    "branch (neither AMEESH_CANON_REF / canon_ref in the host configuration, "
    "nor an already applied commit); the commit read never decides by itself what "
    "is canonical — registry unchanged, \"ameesh canon sync --bootstrap-ref "
    "<branch>\" (logged)")
#: L44 (0031) : même refus pour un canon qui n'est pas le canon par défaut
BOOTSTRAP_REFUSED_OTHER = (
    "first bootstrap of the authenticator registry of canon %s refused: no "
    "trusted canonical branch (neither `ref` in its `canons` entry of the "
    "host configuration, nor an already applied commit of this canon); the commit read "
    "never decides by itself what is canonical — registry of %s unchanged, "
    "\"ameesh canon sync --canon %s --bootstrap-ref <branch>\" (logged)")


class _Refused(Exception):
    """Contrôle refusé sous le verrou : rien n'est écrit."""


class _Concurrent(Exception):
    """Le journal a bougé pendant la transaction : tout est annulé."""


def configured_ref_for(cfg, canon: Canon | None = None) -> str:
    """Branche canonique de confiance que la configuration `cfg` donne à
    `canon` (L44, 0031) : `canon_ref` (AMEESH_CANON_REF) pour le canon par
    défaut (ou sans canon) ; pour un autre canon, la `ref` de SON entrée
    `canons` (reconnue par son chemin), jamais celle du canon par défaut —
    "" si aucune (amorçage explicite, puis manifeste du dernier commit
    appliqué de ce canon)."""
    if canon is None or canon.is_default:
        return str(getattr(cfg, "canon_ref", "") or "")
    for entry in canon_mod.configured(cfg)[1:]:
        if os.path.abspath(os.path.expanduser(entry.path)) == canon.root:
            return str(entry.ref or "")
    return ""


def configured_ref(db: Db, canon: Canon | None = None) -> str:
    """Branche canonique de confiance de la configuration de l'hôte pour
    `canon` (`configured_ref_for`), lue sur la connexion (`db.cfg`)."""
    return configured_ref_for(getattr(db, "cfg", None), canon)


def _config_label(canon: Canon | None) -> str:
    """D'où vient la branche de confiance configurée de ce canon (messages)."""
    if canon is None or canon.is_default:
        return "AMEESH_CANON_REF / canon_ref (host configuration)"
    return "ref of the `canons` entry of canon %s (host configuration)" % canon.id


def last_applied(db: Db, canon: str = "") -> dict | None:
    """Dernière synchronisation appliquée du canon `canon` (son journal
    `authenticator_syncs` ; '' = canon par défaut, L44), ou None."""
    row = storage.of(db).authenticators.last_sync(canon)
    if row is None:
        return None
    commits = row.get("commits")
    if isinstance(commits, str):
        commits = json.loads(commits)
    row["commits"] = {str(k): str(v) for k, v in (commits or {}).items()} \
        if isinstance(commits, dict) else {}
    row["id"] = int(row["id"])
    return row


def _floor_commits(canon: Canon, last: dict) -> list[tuple[canon_mod.Source, str]]:
    """[(source git, dernier commit appliqué de ce membre)] pour ce canon lu."""
    pairs: list[tuple[canon_mod.Source, str]] = []
    if not canon.sources:
        return pairs
    root = canon.sources[0]
    if root.mode == "git" and last.get("root_commit"):
        pairs.append((root, last["root_commit"]))
    floors = last.get("commits") or {}
    for source in canon.sources[1:]:
        floor = floors.get(source.member)
        if source.mode == "git" and floor:
            pairs.append((source, floor))
    return pairs


def _mutable_tracking_ref(source: canon_mod.Source) -> bool:
    """La révision lue est-elle une ref de suivi distante, qu'un fetch avance ?

    `origin/main`, la forme que `canon.load` donne au manifeste : oui. Un SHA,
    une expression de révision, `HEAD`, une branche locale ou une ref explicite :
    non — un fetch ne les avance pas, une relecture seule ne peut donc pas
    rattraper le clone et le refus de monotonie doit rester tel quel (R1 #814)."""
    tracked = canon_mod.remote_branch(source.repo, source.rev)
    if not tracked:
        return False
    name = source.rev
    if name.startswith("refs/remotes/"):
        name = name[len("refs/remotes/"):]
    return name == tracked


def _catch_up_needed(canon: Canon, last: dict) -> tuple[bool, bool]:
    """(relire, fetcher) : ce clone est-il en retard sur le journal ?

    `fetch` quand un dernier commit appliqué est INCONNU du dépôt : un commit
    connu mais absent de ce qui est lu (historique réécrit, `--ref` d'une autre
    branche) ne sera pas apporté par un fetch ; relire suffit à reprendre les
    références qu'un autre passage vient de fetcher (L814).

    `fetch` AUSSI quand un floor CONNU descend strictement la révision lue sur
    une ref de suivi distante : le suivi local est périmé (le commit du journal
    a été vu par un autre chemin : autre ref, aperçu, fetch ciblé), et relire
    sans fetch retrouverait la même révision — la monotonie refuserait à chaque
    passe sans jamais rattraper le clone (R1 #814). Un floor connu absent d'une
    révision divergente, d'un commit explicite ou d'une branche locale n'est pas
    apporté par un fetch : la relecture seule et le refus restent."""
    reread = fetch = False
    for source, floor in _floor_commits(canon, last):
        if source.commit == floor:
            continue                  # déjà ce que le journal a appliqué : rien à lire
        if canon_mod.git_commit(source.repo, floor) is None:
            return True, True
        if not canon_mod.git_contains(source.repo, floor, source.commit):
            reread = True
            if (canon_mod.git_contains(source.repo, source.commit, floor)
                    and _mutable_tracking_ref(source)):
                fetch = True
    return reread, fetch


def _follow_checkout(canon: Canon) -> str:
    """Le clone canonique contient-il la révision lue ? Sinon, l'avance
    (fast-forward seulement) ; rend le refus, à ne pas écrire au registre.

    Un canon illisible ne suit jamais : rien n'a pu être vérifié (R2 cx2)."""
    if not canon.sources:
        return ""
    root = canon.sources[0]
    if root.mode != "git" or root.prefix or canon.untrusted or not canon.readable:
        return ""                      # clone dédié seulement : jamais un dépôt partagé
    head = canon_mod.git_commit(root.repo, "HEAD")
    if head and canon_mod.git_contains(root.repo, root.commit, head):
        return ""                      # le checkout contient déjà la révision lue
    error = canon_mod.git_fast_forward(root.repo, root.commit)
    if error:
        # un autre passage a pu avancer le checkout entre-temps (plusieurs
        # exécuteurs par hôte) : le refus ne vaut que si HEAD ne contient
        # toujours pas la révision lue
        head = canon_mod.git_commit(root.repo, "HEAD")
        if head and canon_mod.git_contains(root.repo, root.commit, head):
            error = ""
    if not error:
        # l'écart signalé à la lecture n'existe plus ; les modifications
        # locales, elles, restent signalées (elles n'ont pas été touchées)
        canon.load_findings[:] = [f for f in canon.load_findings
                                  if f.code != "canon-local-divergence"]
        return ""
    head = canon_mod.git_commit(root.repo, "HEAD") or "?"
    return ("canon checkout %s @ %s does not contain the applied revision %s and "
            "cannot fast-forward it (%s): authenticator registry unchanged — bring "
            "the checkout up to date (git -C %s merge --ff-only %s)"
            % (root.repo, head[:12], root.commit[:12], error, root.repo,
               root.commit[:12]))


def catch_up(db: Db, canon: Canon, host: str, key: str | None = None, *,
             trusted_ref: str | None = None,
             bootstrap_ref: str = "") -> tuple[Canon, str]:
    """Rattrape le clone de l'hôte sur le journal du canon (L814).

    Le journal des authentificateurs est unique par canon, tous hôtes
    confondus : un hôte dont le clone n'a pas fetché lit un commit antérieur
    au dernier commit appliqué et `_check_descends` le refuse — sans jamais
    pouvoir se rattraper, aucune passe ne fetchant d'elle-même. Ici, quand un
    dernier commit appliqué est inconnu du dépôt, les dépôts du canon sont
    fetchés et le canon relu UNE fois ; quand il est connu mais absent de ce
    qui est lu, le canon est relu — avec fetch si un floor connu descend la
    révision lue sur une ref de suivi distante (suivi local périmé, `_catch_up_needed`),
    sans fetch sinon (historique divergent, `--ref` explicite ou branche locale).

    Puis le checkout du clone canonique suit la révision lue
    (`canon.git_fast_forward`, fast-forward seulement) — mais seulement après
    les contrôles de confiance et de monotonie (`_verify`) sur le canon relu :
    une révision non fusionnée lue par `--ref` ne doit pas avancer le checkout
    avant d'être refusée (relecture R1/R2 du 13/10). Un canon illisible, à
    l'entrée comme après la relecture, n'avance jamais le checkout : la lecture
    en échec conserve la source racine, mais rien n'a pu être vérifié et le
    refus `CanonUnreadable` suit (R2 cx2, 13/10). `trusted_ref` (None : la
    configuration de l'hôte pour ce canon) et `bootstrap_ref` sont ceux de
    l'appelant ; ils refont foi sous le verrou (`_sync_locked`). Rend
    `(canon, refus)` : `refus` n'est non vide que si le canon relu ne passe
    pas ces contrôles, ou si le checkout ne peut pas contenir cette révision
    (clone divergé, modifications locales écrasées) — `sync_authenticators`
    refuse alors d'écrire, registre et checkout inchangés.

    Lecture seule pour le registre : l'appelant reste seul juge (`_verify`,
    sous le verrou, relit le journal)."""
    if key is None:
        key = state_key(db, canon, host)
    if trusted_ref is None:
        trusted_ref = configured_ref(db, canon)
    last = last_applied(db, key)
    if last is not None:
        reread, fetch = _catch_up_needed(canon, last)
        if reread or fetch:
            canon = canon_mod.load(canon.root, ref=canon.rev, untrusted=canon.untrusted,
                                   fetch=fetch, default=canon.is_default)
    # L814 (R1/R2) : contrôles AVANT toute mutation du checkout — sinon un ref
    # non fusionné, descendant du dernier appliqué, avance HEAD puis se fait
    # refuser : registre intact mais canon local modifié.
    if not canon.readable:
        # R2 cx2 : la source racine est ajoutée avant la lecture des membres et
        # des fiches ; une GitError/OSError ultérieure rend `readable` faux sans
        # vider `sources`. Rien n'a pu être vérifié (ni branche canonique, ni
        # monotonie) : le checkout ne bouge pas, la passe refuse ensuite
        # (CanonUnreadable). Un `--ref` descendant non fusionné ne déplace donc
        # jamais HEAD.
        return canon, ""
    if not canon.untrusted and canon.sources:
        try:
            _verify(canon, last, trusted_ref, bootstrap_ref)
        except _Refused as exc:
            return canon, str(exc)
    return canon, _follow_checkout(canon)


def _applied_branch(root: canon_mod.Source, last: dict) -> str:
    """La branche canonique selon le manifeste du DERNIER commit appliqué
    (`ref` de son membre racine), sinon celle contre laquelle ce commit a été
    vérifié. Un changement de branche n'y entre qu'une fois fusionné."""
    commit = last["root_commit"]
    if not canon_mod.git_commit(root.repo, commit):
        raise _Refused(
            "canon behind the authenticator registry: the last applied commit "
            "%s is unknown to this clone — registry unchanged, ameesh canon sync "
            "--fetch" % commit[:12])
    path = (root.prefix + "/" if root.prefix else "") + "federation.yaml"
    raw = canon_mod.git_read(root.repo, commit, path)
    if raw is None:
        return last["branch"]
    try:
        manifest = canon_mod.load_yaml(raw.decode("utf-8"))
    except (UnicodeDecodeError, canon_mod.YamlError) as exc:
        raise _Refused("unreadable federation.yaml at the last applied commit %s (%s): "
                       "canonical branch unknown, registry unchanged" % (commit[:12], exc))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("members") or [], list):
        raise _Refused("invalid federation.yaml at the last applied commit %s: canonical "
                       "branch unknown, registry unchanged" % commit[:12])
    root_id = str(manifest.get("root") or "canon")
    entry = next((m for m in manifest.get("members") or []
                  if isinstance(m, dict) and str(m.get("id")) == root_id), None)
    ref = str((entry or {}).get("ref") or "")
    if not ref:
        return last["branch"]
    branch = canon_mod.remote_branch(root.repo, ref)
    if branch is None:
        raise _Refused("ref %r of root member %s (manifest of the last applied commit "
                       "%s): not a branch, registry unchanged" % (ref, root_id, commit[:12]))
    return branch


def _trusted_branch(root: canon_mod.Source, last: dict | None, trusted_ref: str,
                    bootstrap_ref: str,
                    canon: Canon | None = None) -> tuple[str, str, list[str]]:
    """(branche canonique de confiance, origine, remarques) — jamais tirée du
    commit lu : configuration de l'hôte (pour CE canon, L44), sinon manifeste
    du dernier commit appliqué de ce canon, sinon amorçage explicite ; sinon
    refus."""
    notes: list[str] = []
    if trusted_ref:
        branch = canon_mod.remote_branch(root.repo, trusted_ref)
        if branch is None:
            raise _Refused("%r (%s) does not name a branch: trusted canonical branch "
                           "unknown, registry unchanged"
                           % (trusted_ref, _config_label(canon)))
        trust = TRUST_CONFIG
    elif last is not None:
        branch, trust = _applied_branch(root, last), TRUST_APPLIED
    elif bootstrap_ref:
        branch = canon_mod.remote_branch(root.repo, bootstrap_ref)
        if branch is None:
            raise _Refused("--bootstrap-ref %r does not name a branch: registry "
                           "unchanged" % bootstrap_ref)
        trust = TRUST_BOOTSTRAP
        notes.append("BOOTSTRAP of the authenticator registry: trusted canonical "
                     "branch %s set by --bootstrap-ref (logged)" % branch)
    else:
        if canon is not None and not canon.is_default:
            raise _Refused(BOOTSTRAP_REFUSED_OTHER % (canon.id, canon.id, canon.id))
        raise _Refused(BOOTSTRAP_REFUSED)
    if bootstrap_ref and trust != TRUST_BOOTSTRAP:
        notes.append("--bootstrap-ref %s ignored: %s" % (
            bootstrap_ref, "registry already bootstrapped" if trust == TRUST_APPLIED
            else "the host configuration sets the canonical branch"))
    return branch, trust, notes


def _check_on_branch(source: canon_mod.Source, branch: str, why: str) -> None:
    """Le commit lu est-il atteignable depuis la branche canonique de confiance ?"""
    head = canon_mod.git_commit(source.repo, "refs/remotes/" + branch)
    if not head:
        raise _Refused("%s: canonical branch %s (%s) not found in %s (git fetch?) — "
                       "authenticator registry unchanged"
                       % (source.member, branch, why, source.repo))
    if not canon_mod.git_contains(source.repo, source.commit, head):
        raise _Refused("%s @ %s: commit not on the canonical branch %s (%s, %s) — "
                       "non-canonical revision, authenticator registry unchanged"
                       % (source.member, source.commit[:12], branch, head[:12], why))


def _check_descends(source: canon_mod.Source, floor: str | None) -> None:
    """Monotonie : jamais d'écriture d'un commit qui ne descend pas du dernier appliqué."""
    if floor and not canon_mod.git_contains(source.repo, floor, source.commit):
        raise _Refused(
            "%s @ %s does not descend from the last applied commit %s: canon behind "
            "(ameesh canon sync --fetch) or rewritten history — never going "
            "backwards, authenticator registry unchanged"
            % (source.member, source.commit[:12], floor[:12]))


def _verify(canon: Canon, last: dict | None, trusted_ref: str,
            bootstrap_ref: str) -> tuple[str, str, list[str]]:
    """Contrôles de confiance (B2) et de monotonie (B1), sous le verrou.

    1. branche canonique de confiance (`_trusted_branch`) ; le commit racine lu
       doit en être atteignable (suivi distant `refs/remotes/<branche>`) ;
    2. il doit descendre du dernier commit racine appliqué ;
    3. chaque autre source (dépôt distinct) : atteignable depuis la branche de
       son entrée au manifeste — celui du commit racine, désormais vérifié
       (fusionné) — et descendante de son dernier commit appliqué.
    """
    root = canon.sources[0]
    branch, trust, notes = _trusted_branch(root, last, trusted_ref, bootstrap_ref, canon)
    _check_on_branch(root, branch, TRUST_LABELS[trust])
    if last is not None:
        _check_descends(root, last["root_commit"])
    manifest = canon.federation if isinstance(canon.federation, dict) else {}
    for source in canon.sources[1:]:
        if source.repo == root.repo and source.commit == root.commit:
            continue                    # même instantané que la racine : déjà vérifié
        entry = next((m for m in manifest.get("members") or []
                      if isinstance(m, dict) and str(m.get("id")) == source.member), None)
        ref = str((entry or {}).get("ref") or canon_mod.DEFAULT_BRANCH)
        member_branch = canon_mod.remote_branch(source.repo, ref)
        if member_branch is None:
            raise _Refused("%s: manifest ref %r is not a branch — registry unchanged"
                           % (source.member, ref))
        _check_on_branch(source, member_branch, "manifest of root commit %s"
                         % root.commit[:12])
        if last is not None:
            _check_descends(source, last["commits"].get(source.member))
    return branch, trust, notes


def _record(db: Db, last: dict | None, canon: Canon, branch: str, trust: str, host: str,
            result: dict, key: str = "") -> None:
    """Journalise la synchronisation appliquée (dernier commit appliqué) dans
    le journal du canon `key` (L44), en vérifiant que ce journal n'a pas bougé
    depuis sa lecture sous le verrou."""
    root = canon.sources[0]
    commits = dict(last["commits"]) if last is not None else {}
    commits.update({s.member: s.commit for s in canon.sources if s.mode == "git"})
    changed = any(result.get(key) for key in ("added", "updated", "revoked"))
    expected = last["id"] if last is not None else None
    if last is not None and not changed and (
            last["root_commit"], last["commits"], last["branch"]) == (root.commit, commits,
                                                                      branch):
        head = storage.of(db).authenticators.journal_head(key)
        if head is None or head != expected:
            raise _Concurrent("sync journal modified during the transaction")
        return
    summary = {key: len(result.get(key) or []) for key in ("added", "updated", "revoked",
                                                           "frozen", "errors")}
    appended = storage.of(db).authenticators.append_sync(
        root_member=root.member, root_commit=root.commit,
        commits_json=json.dumps(commits, sort_keys=True), branch=branch, trust=trust,
        host=host or "", summary_json=json.dumps(summary, sort_keys=True),
        expected=expected, canon=key)
    if not appended:
        raise _Concurrent("sync journal modified during the transaction")


def _lag(db: Db, canon: Canon, key: str = "") -> tuple[list[str], set[str]]:
    """Le canon lu est-il en retard sur ce que le registre a déjà appliqué ?

    Chaque ligne écrite par une synchronisation porte le commit qui l'a
    écrite (ajout, mise à jour, révocation). Un hôte dont le clone n'a pas
    encore vu ce commit (pas de `--fetch`) ne doit pas réappliquer un canon
    plus ancien : il réactiverait un authentificateur retiré depuis. Rend
    (lignes en avance sur le canon lu, sources du registre absentes du canon
    lu). Une ligne hors du format des fiches (écrite à la main) n'est pas un
    repère : la synchronisation la traite comme L6 le prévoit (révoquée si le
    canon ne la déclare pas). L44 (0031) : seules les lignes du canon `key`
    (colonne `canon`) sont des repères.
    """
    sources = {s.member: s for s in canon.sources if s.mode == "git"}
    rows = storage.of(db).authenticators.canon_refs(receipts.SYNC_REVOCATIONS, key)
    ahead: list[str] = []
    unread: set[str] = set()
    seen: dict[tuple[str, str], bool] = {}
    for row in sorted(rows, key=lambda r: r["canon_ref"] or ""):
        match = receipts.FICHE_REF_RE.fullmatch(row["canon_ref"] or "")
        if not match or match.group("canon") not in (None, canon.id):
            # hors format (écrite à la main), ou référence d'un autre canon :
            # pas un repère de ce canon (une ligne rattachée par un changement
            # de canon par défaut garde sa référence sans préfixe)
            continue
        member, commit = match.group("member"), match.group("commit")
        source = sources.get(member)
        if source is None:
            unread.add(member)
            continue
        key = (member, commit)
        if key not in seen:
            seen[key] = canon_mod.git_contains(source.repo, commit, source.commit)
        if not seen[key]:
            ahead.append(row["canon_ref"])
    return ahead, unread


def _partial(canon: Canon, unread: set[str]) -> list[str]:
    """Raisons de croire qu'une fiche Member a pu échapper à la lecture."""
    reasons = ["%s (%s)" % (f.code, f.where()) for f in canon.load_findings
               if f.severity == canon_mod.ERROR or f.code == "member-absent"]
    reasons += ["registry source %s not read" % member for member in sorted(unread)]
    return reasons


def registry_members(canon: Canon) -> list[dict]:
    """Les fiches Member au format de `receipts._apply_authenticators`.

    `canon_ref` = la fiche lue (`<membre>:<chemin>@<commit>`). La liste
    `authenticators` est passée telle qu'écrite dans la fiche (absente,
    invalide : L6 gèle le membre) ; une entrée ne choisit jamais sa propre
    provenance (`canon_ref` d'entrée retiré : c'est la fiche qui fait foi).
    """
    members = []
    for member in canon.members:
        entry: dict = {"title": member.title, "canon_ref": member.fiche.ref}
        if "authenticators" in member.fiche.data:
            raw = member.fiche.data["authenticators"]
            if isinstance(raw, list):
                raw = [{k: v for k, v in item.items() if k != "canon_ref"}
                       if isinstance(item, dict) else item for item in raw]
            entry["authenticators"] = raw
        members.append(entry)
    return members


def sync_authenticators(db: Db, canon: Canon, *, trusted_ref: str | None = None,
                        bootstrap_ref: str = "", host: str = "",
                        key: str | None = None, checkout_block: str = "") -> AuthenticatorSync:
    """Canon → registre des authentificateurs (§8.2) : le SEUL point d'entrée.

    Écriture de confiance. Rien n'est écrit si le canon est illisible ou lu
    hors git (non approuvé). Sinon, UNE transaction
    (`storage.of(db).authenticators.under_registry_lock`, les deux pilotes) :

    1. verrou consultatif transactionnel du registre (première instruction
       de la transaction, qui rend son jeton), pris AVANT tout contrôle :
       deux synchronisations, de deux clones ou de deux hôtes du même canon,
       se sérialisent ;
    2. sous ce verrou, relecture du dernier commit appliqué (`last_applied`)
       et contrôles (`_verify`) : branche canonique de CONFIANCE — jamais
       tirée du commit lu (`trusted_ref` = configuration de l'hôte, sinon
       manifeste du dernier commit appliqué, sinon `bootstrap_ref` explicite,
       sinon refus) —, commit lu atteignable depuis elle, et descendant du
       dernier commit appliqué (monotonie : jamais de retour en arrière) ;
       puis le canon ne doit pas être en retard sur une ligne du registre
       (`_lag`) ;
    3. écritures de L6 par `receipts._apply_authenticators` (jeton du verrou
       exigé, verrou revérifié dans `pg_locks` ; entrée invalide : membre
       gelé ; absent du canon : révoqué ; canon lu en partie : aucune
       révocation pour absence) et journal (`authenticator_syncs`), puis
       COMMIT.

    `trusted_ref` None (défaut) : la configuration de la connexion pour CE
    canon (`configured_ref(db, canon)`) ; "" : aucune. Un refus rend
    AUTH_REFUSED et n'écrit rien ; une erreur de base annule la transaction
    entière.

    L44 (0031) : `key` est le canon du registre et du journal ('' = canon par
    défaut ; None : `state_key(db, canon, host)`, la clé de son état). Tout
    — journal, monotonie, retard, révocations — est borné à ce canon.

    `checkout_block` (L814) : le clone de l'hôte ne peut pas contenir la
    révision lue (clone divergé, modifications locales) — un refus y est
    inscrit AVANT l'écriture, le registre reste inchangé (`catch_up`).
    """
    if key is None:
        key = state_key(db, canon, host)
    if trusted_ref is None:
        trusted_ref = configured_ref(db, canon)
    if not canon.readable:
        return AuthenticatorSync(AUTH_SKIPPED, "unreadable canon: registry unchanged",
                                 canon=key)
    if canon.untrusted:
        return AuthenticatorSync(AUTH_SKIPPED, "canon NOT APPROVED (working files): "
                                               "authenticator registry unchanged",
                                 canon=key)
    if not canon.sources:
        return AuthenticatorSync(AUTH_SKIPPED, "no source read: registry unchanged",
                                 canon=key)
    try:
        return storage.of(db).authenticators.under_registry_lock(
            lambda lock: _sync_locked(lock, canon, trusted_ref, bootstrap_ref, host, key,
                                      checkout_block))
    except receipts.RegistryLockError as exc:
        return AuthenticatorSync(AUTH_REFUSED, "%s: transaction rolled back" % exc, canon=key,
                                 transient=True)
    except _Concurrent as exc:
        return AuthenticatorSync(AUTH_REFUSED, "%s: transaction rolled back, authenticator "
                                               "registry unchanged" % exc, canon=key)
    except DbError as exc:
        return AuthenticatorSync(AUTH_REFUSED, "database: %s — transaction rolled back, "
                                               "authenticator registry unchanged" % exc,
                                 canon=key, transient=transient_db_error(exc))
    except ValueError as exc:
        return AuthenticatorSync(AUTH_REFUSED, "module policy: %s — transaction rolled back" % exc,
                                 canon=key)


def _sync_locked(lock: receipts.RegistryLock, canon: Canon, trusted_ref: str,
                 bootstrap_ref: str, host: str, key: str = "",
                 checkout_block: str = "") -> AuthenticatorSync:
    """Contrôles et écritures, dans la transaction et sous le verrou du
    registre (`lock` : son jeton ; `lock.db` : la transaction) — pour le
    registre et le journal du canon `key` seulement (L44).

    `checkout_block` (L814) : refus d'`catch_up` — contrôles faits, le clone de
    l'hôte ne contient pas la révision lue ; rien n'est écrit."""
    tx = lock.db
    last = last_applied(tx, key)
    try:
        branch, trust, notes = _verify(canon, last, trusted_ref, bootstrap_ref)
    except _Refused as exc:
        return AuthenticatorSync(AUTH_REFUSED, str(exc), canon=key)
    if checkout_block:
        return AuthenticatorSync(AUTH_REFUSED, checkout_block, trust=trust, branch=branch,
                                 notes=notes, canon=key)
    ahead, unread = _lag(tx, canon, key)
    if ahead:
        return AuthenticatorSync(AUTH_REFUSED, (
            "canon behind the authenticator registry (already applied: %s): "
            "registry unchanged — ameesh canon sync --fetch" % ", ".join(ahead[:5])),
            trust=trust, branch=branch, notes=notes, canon=key)
    partial = _partial(canon, unread)
    from . import modules as project_modules
    try:
        project_modules.declarations(canon.federation, canon)
    except ValueError as exc:
        return AuthenticatorSync(AUTH_REFUSED, str(exc), canon=key)
    # contrôles faits, verrou détenu : la seule écriture du registre (de CE canon)
    result = receipts._apply_authenticators(
        lock, registry_members(canon), revoke_absent=not partial,
        source_commits={s.member: s.commit for s in canon.sources}, canon=key)
    if not partial:
        project_modules.sync(tx, canon)
    _record(tx, last, canon, branch, trust, host, result, key)
    return AuthenticatorSync(AUTH_SYNCED, "", result, partial, trust=trust, branch=branch,
                             notes=notes, canon=key)


def _member_of(canon_ref: str | None) -> str:
    """Membre d'une référence de fiche, sans le préfixe de canon (L42)."""
    head = (canon_ref or "").split(":", 1)[0]
    return head.split("/", 1)[-1]


#: constat d'un nom déjà déclaré par un autre canon (L42, 0031)
NAME_CONFLICT = "canon-name-conflict"


def _released(row: dict) -> bool:
    """Ligne LIBÉRÉE : arrêtée par sync parce que retirée de son canon (L42) ;
    un autre canon peut en reprendre le nom."""
    return row.get("status") == "stopped" \
        and (row.get("status_text") or "").startswith(STOP_MARK)


def _owner_label(value: str | None) -> str:
    return "canon %s" % value if value else "the default canon"


def _write_declared(db: Db, name: str, values: dict) -> bool:
    """Écrit les colonnes déclaratives ; `placement_profile` est le profil des
    valeurs écrites (celles que `placement.evaluate` a jugées), calculé par
    `ameesh_placement_profile` dans la même instruction."""
    return storage.of(db).canon.write_declared(name, values)


def _clear_responsible(db: Db, name: str, host: str, owner=None) -> None:
    storage.of(db).canon.clear_responsible(name, host, owner)


def _set_placement(db: Db, name: str, host: str, verdict: placement_mod.Verdict) -> None:
    """Écrit un verdict de placement et son profil évalué (colonnes
    déclaratives seulement), s'il change.

    `verdict.profile` : le profil évalué (texte de `ameesh_placement_profile`).
    None pour un refus jugé sur la ligne telle qu'elle est : son profil
    courant, calculé dans la même instruction. Un verdict admis n'est jamais
    écrit sans profil évalué (il vaudrait pour n'importe quel profil).
    """
    if verdict.ok and verdict.profile is None:
        raise ValueError("admitted placement verdict without evaluated profile: %s" % name)
    storage.of(db).canon.set_placement(name, host, ok=verdict.ok,
                                       diagnostic=verdict.diagnostic, ref=verdict.ref,
                                       profile=verdict.profile)


def _write_candidate(db: Db, canon: Canon, agent, admission, host: str, *,
                     canon_key: str, forge=None) -> None:
    """L165 (EP1) : verdict de placement de `agent` sur l'hôte candidat `host`.

    Le verdict est évalué comme celui de l'hôte courant (politique de l'hôte
    et règle de visibilité), mais écrit dans `agent_candidates` : c'est lui
    que la réclamation depuis `host` compare. Ne déplace jamais la ligne.
    """
    verdict = placement_mod.evaluate(canon, agent.title, host)
    visibility_mod.annotate(canon, agent, host, verdict, db=db, forge=forge)
    cwd, _ = canon_mod.work_dir_for(canon.host(host), agent, admission)
    storage.of(db).canon.write_candidate(
        agent.title, host, canon=canon_key,
        cwd=os.path.expanduser(cwd) if cwd else None,
        ok=verdict.ok, diagnostic=verdict.diagnostic, ref=verdict.ref,
        harness=agent.harness or "other", provider=agent.provider,
        model=agent.model,
        credential_mode=admission.credential_mode or agent.credential_mode)


def _card_error_text(errors) -> str:
    """Texte court et stable d'une erreur de fiche Agent (codes, sans contenu)."""
    return "agent card invalid: " + "; ".join(sorted({f.code for f in errors}))


def _close_card_candidates(db: Db, name: str, canon_key: str, errors) -> None:
    """L165 (EP1) : une fiche Agent fautive ferme TOUS ses candidats.

    L'erreur est portée par la fiche (responsable non résolu, candidat non
    admis, relève illisible), donc elle vaut sur chaque hôte : le verdict
    local d'un candidat ne suffit pas à rouvrir la réclamation depuis un autre.
    """
    storage.of(db).canon.close_candidates(
        name, canon=canon_key, diagnostic=_card_error_text(errors))


def _lineage_root(by_name: dict[str, dict], creator: str | None) -> tuple[dict | None, str]:
    """Créateur racine d'un éphémère, suivi comme `registry.canon_governed_sql`.

    (ligne, '') : premier ancêtre non éphémère ou venu du canon ;
    (None, raison) : lignée rompue ou trop longue (tenue pour gouvernée).
    """
    name = creator
    for _depth in range(registry._LINEAGE_MAX):
        row = by_name.get(name) if name else None
        if row is None:
            return None, "creator %s not found" % (name or "undeclared")
        if not row.get("ephemeral") or row.get("canon_ref"):
            return row, ""
        name = row.get("created_by")
    return None, "lineage of more than %d creators" % registry._LINEAGE_MAX


def _inherit_placements(db: Db, host: str, canon: Canon | None = None) -> None:
    """Les éphémères de cet hôte héritent du verdict de placement de leur
    créateur racine du canon (C4), à condition d'avoir le profil que ce
    verdict a jugé : même hôte, même harnais, même fournisseur, même modèle,
    même mode (profil courant de l'éphémère = profil évalué du créateur).
    Sinon, refusé : « profil divergé ». Un éphémère issu d'un agent inscrit à
    la main n'est pas gouverné : il n'est pas touché. Ni action, ni colonne
    d'état."""
    rows = storage.of(db).canon.lineage_rows()
    by_name = {row["name"]: row for row in rows}
    for row in sorted(rows, key=lambda r: r["name"]):
        if not row.get("ephemeral") or row.get("host") != host:
            continue
        if canon is not None and not canon.owns(row.get("canon")):
            continue                    # L42 : éphémère d'un autre canon, pas touché
        root, broken = _lineage_root(by_name, row.get("created_by"))
        if root is not None and not root.get("canon_ref"):
            continue                    # lignée d'un agent inscrit à la main
        if root is None:
            verdict = placement_mod.Verdict(
                row["name"], host, False,
                "unverifiable lineage (%s): placement not inherited" % broken)
        elif root.get("host") != host:
            verdict = placement_mod.Verdict(
                row["name"], host, False,
                "creator %s placed on %s, not on %s" % (
                    root["name"], root.get("host") or "?", host),
                root.get("placement_ref"))
        elif root.get("placement_ok") \
                and row.get("profile_now") != root.get("placement_profile"):
            # le verdict admis du créateur a jugé un autre profil que celui de
            # l'éphémère : rien n'en est hérité
            verdict = placement_mod.Verdict(
                row["name"], host, False,
                "profile diverged from the one evaluated for creator %s (evaluated: %s; "
                "ephemeral: %s): placement not inherited" % (
                    root["name"], root.get("placement_profile") or "none",
                    row.get("profile_now")),
                root.get("placement_ref"), profile=row.get("profile_now"))
        else:
            verdict = placement_mod.Verdict(
                row["name"], host, root.get("placement_ok"),
                root.get("placement_diagnostic") or "", root.get("placement_ref"),
                profile=root.get("placement_profile"))
        written = verdict.profile if verdict.profile is not None else row.get("profile_now")
        if (row.get("placement_ok"), row.get("placement_diagnostic") or "",
                row.get("placement_ref"), row.get("placement_profile")) != (
                verdict.ok, verdict.diagnostic, verdict.ref, written):
            _set_placement(db, row["name"], host, verdict)


def _rows(db: Db, host: str, names: list[str]) -> dict[str, dict]:
    return {row["name"]: row for row in storage.of(db).canon.host_rows(host, names)}


def _same(row: dict, values: dict) -> bool:
    # profil évalué absent, ou différent du profil courant (posé à la main) :
    # à réécrire, même si toutes les autres colonnes sont égales
    if not row.get("profile_fresh"):
        return False
    for key in DECLARATIVE:
        old, new = row.get(key), values.get(key)
        if key == "budget_usd":
            old = None if old is None else float(old)
            new = None if new is None else float(new)
        if key in ("capabilities", "projects", "roles", "relit"):
            old = None if old is None else list(old)
        if old != new:
            return False
    return True


def periodic_sync(db: Db, canon: Canon, host: str, interval_s: float, *,
                  on_incomplete=None) -> SyncReport | None:
    """#3963: one complete sync per host/canon/interval, without a long lock.

    Other executors reuse only a completed result for the same immutable
    sources and trust configuration. Pending, failed or different snapshots
    never settle the host gate. Explicit CLI sync remains unconditional.
    """
    if interval_s <= 0 or canon.untrusted or not canon.readable or not canon.sources:
        return sync(db, canon, host)
    key = state_key(db, canon, host)
    signature_payload = {
        "sources": [(s.member, s.commit, s.mode) for s in canon.sources],
        "trusted_ref": configured_ref(db, canon),
        "id": canon.id, "default": canon.is_default,
        "findings": [str(f) for f in canon.load_findings],
    }
    signature = hashlib.sha256(json.dumps(signature_payload, sort_keys=True).encode()).hexdigest()
    store = storage.of(db).canon
    token = uuid.uuid4().hex
    if not store.claim_sync(host, key, token, interval_s):
        shared = store.shared_sync(host, key)
        if shared is None or shared["sync_signature"] != signature:
            if on_incomplete is not None:
                reason = ("shared sync pending or failed (no completed token)"
                          if shared is None else
                          "snapshot mismatch: local signature %s, shared signature %s"
                          % (signature, shared["sync_signature"]))
                sources = ", ".join("%s=%s@%s (%s)" % (
                    s.member, s.repo, s.commit, s.mode) for s in canon.sources)
                on_incomplete("canon %s at %s: %s; sources [%s]; "
                              "claim interval %.1fs; local signature payload %s" % (
                                  canon.id, canon.root, reason, sources, interval_s,
                                  json.dumps(signature_payload, sort_keys=True)))
            return None
        return SyncReport(host, str(canon.root), False, [], [],
                          status=shared["status"], diagnostic=shared["diagnostic"], shared=True)
    report = sync(db, canon, host)
    store.complete_sync(host, key, token, signature)
    return report


def sync(db: Db, canon: Canon, host: str, findings: list[Finding] | None = None, *,
         trusted_ref: str | None = None, bootstrap_ref: str = "",
         forge=None, allow_rewind: bool = False) -> SyncReport:
    """Aligne le registre de `host` sur le canon et enregistre l'état du canon.

    Canon illisible : seul l'état `unreadable` est écrit, puis CanonUnreadable.
    Ordre fail closed : un état non `ok` est écrit AVANT toute autre écriture
    (la réclamation est fermée d'abord) ; un état `ok` APRÈS (elle ne rouvre
    qu'une fois le registre aligné).

    `allow_rewind` (#3955, `canon sync --allow-rewind`) : déclare un retour
    arrière du commit audité. Sans lui, `record_state` refuse d'enregistrer un
    commit qui ne descend pas du dernier commit audité ; avec lui, le retour
    est accepté et journalisé (thread).

    `trusted_ref` (configuration de l'hôte) et `bootstrap_ref` (premier
    amorçage explicite) ne servent qu'au registre des authentificateurs :
    voir `sync_authenticators`. `trusted_ref` non fourni (None) : sync le
    prend lui-même dans la configuration de la connexion, POUR CE CANON
    (`configured_ref(db, canon)` : `canon_ref` / AMEESH_CANON_REF pour le
    canon par défaut, la `ref` de son entrée `canons` pour un autre, L44) —
    l'exécuteur, qui ne le passe pas, suit donc la même priorité que
    `ameesh canon sync` ; "" : aucune.

    L'issue des authentificateurs est inscrite dans `canon_state`
    (`auth_status`, `auth_diagnostic`) et, en cas d'erreur, dans le fil
    (`record_authenticators`) : jamais silencieuse, quel que soit l'appelant.
    """
    if trusted_ref is None:
        trusted_ref = configured_ref(db, canon)
    # L780 : un processus plus vieux que le paquet installé ne juge pas le
    # canon — son validateur peut refuser un descripteur neuf légitime
    # (`codex.json` : `modes` sans `flag`, refusé par l'ancien code). Rien
    # n'est écrit : `canon_state`, responsables et placements restent au
    # dernier passage valide, l'hôte se ferme (fail closed) le temps du
    # redémarrage de l'exécuteur.
    stale = harnesses.stale_reason()
    if stale:
        raise CanonStale(host, stale)
    # L42 (0031) : clé d'état et identité effective de CE canon ; seules ses
    # lignes (colonne `canon`, NULL = canon par défaut) sont lues et écrites
    key = state_key(db, canon, host)
    # L814 : le journal des authentificateurs est global par canon ; la passe
    # rattrape le clone (fetch, relecture, fast-forward du checkout) AVANT de
    # le juger — sinon un hôte dont le clone n'a pas fetché refuse sans fin.
    # Les contrôles de confiance et de monotonie passent avant le fast-forward
    # (R1/R2) ; `checkout_block` non vide : contrôle refusé ou checkout qui ne
    # peut pas contenir la révision lue, les authentificateurs refuseront
    # d'écrire (registre et checkout inchangés).
    canon, checkout_block = catch_up(db, canon, host, key, trusted_ref=trusted_ref,
                                     bootstrap_ref=bootstrap_ref)
    ident = key or canon.id
    owner = (ident, canon.is_default)

    def mine(row: dict) -> bool:
        value = row.get("canon")
        return (canon.is_default and not value) or value == ident

    if not canon.readable:
        status, diagnostic = assess(canon, host)
        record_state(db, host, status, diagnostic, canon, key=key,
                     allow_rewind=allow_rewind)
        raise CanonUnreadable(host, diagnostic, list(canon.load_findings))
    if findings is None:
        findings = canon_mod.validate(canon)
    findings = list(findings)
    from .agent_names import conflicts
    findings.extend(conflicts(db, canon))
    status, diagnostic = assess(canon, host, findings)
    if status != CANON_OK:
        record_state(db, host, status, diagnostic, canon, key=key,
                     allow_rewind=allow_rewind)
    block = canon_mod.blocking(findings)
    #: L780 : le canon est invalide POUR CET HÔTE (erreur sans sujet, ou liée à
    #: sa fiche Host). La réclamation est déjà fermée par `canon_state` : une
    #: telle erreur ne doit pas détruire le dernier responsable résolu.
    host_wide = bool(block.global_errors or block.by_host.get(host))
    #: L165 : membres réellement lus, pour ne jamais retirer un rôle élu sur
    #: la foi d'une lecture locale incomplète (clone ou membre fédéré absent).
    loaded = canon.loaded_members()
    actions: list[SyncAction] = []

    # L31 (0029) : une fiche `Placement` est une ADMISSION (hôtes nommés ou
    # étiquettes). L'hôte d'une ligne du registre est un ÉTAT d'exécution : on
    # ne déplace jamais un agent dont l'hôte courant est admis.
    admitted_here: dict[str, canon_mod.Placement] = {}
    other_admitted: dict[str, list[str]] = {}
    for agent in canon.agents:
        admission = placement_mod.admission_of(canon, agent.title)
        if admission is None:
            continue
        admis = placement_mod.admitted_hosts(canon, admission)
        if host in admis:
            admitted_here.setdefault(agent.title, admission)
        if admis:
            other_admitted[agent.title] = [h for h in admis if h != host]

    # L165 (EP1) : la relève et les candidats sont déclaratifs, comme le
    # placement. Écrits avant la lecture pour que `_same` voie la fiche du
    # jour ; `set_election` ne déplace jamais l'agent et retire les verdicts
    # des hôtes qui ne sont plus candidats.
    canon_key = "" if canon.is_default else ident
    for declared in canon.agents:
        storage.of(db).canon.set_election(
            declared.title, declared.releve, declared.candidats, canon=canon_key)
    # L165 (EP1) : un agent élu peut avoir sa ligne chez un AUTRE candidat.
    # Le sync de cet hôte doit quand même voir les fiches retirées (pour
    # fermer leur candidature) et les candidats qui ne sont plus admis ici.
    stale_candidates = storage.of(db).canon.candidate_names(host, canon_key)
    removed_candidates = sorted(name for name in stale_candidates
                                if canon.agent(name) is None)
    for stale in stale_candidates:
        if stale in admitted_here:
            continue
        declared = canon.agent(stale)
        known = registry.get(db, stale)
        cross = known is not None and (known.get("host") or host) != host
        if cross and (block.global_errors
                      or _member_of(known.get("canon_ref")) not in loaded):
            # Lecture locale incomplète : ne pas retirer le rôle au détenteur
            # sain ; son propre sync (ou une lecture complète) jugera.
            continue
        if declared is None:
            # Fiche retirée : fermer la candidature tout de suite, même si la
            # ligne appartient désormais à un autre canon ; la boucle de
            # retrait arrête ensuite l'agent sur l'hôte de sa ligne.
            storage.of(db).canon.set_election(stale, "hote", None, canon=canon_key)
            continue
        errors = block.by_agent.get(stale, ())
        if errors:
            _close_card_candidates(db, stale, canon_key, errors)
            if known is not None and mine(known):
                _clear_responsible(db, stale, known.get("host") or host, owner)
        else:
            storage.of(db).canon.clear_candidate(stale, host, canon=canon_key)
    rows = _rows(db, host, sorted(set(admitted_here) | set(other_admitted)
                                  | set(removed_candidates)))

    #: #538 : les agents réellement gouvernés par CE canon sur CET hôte — les
    #: seuls destinataires d'une poussée de règles (jamais un agent écarté pour
    #: un autre canon, ni un agent exécuté sur un autre hôte admis).
    process_agents: set[str] = set()
    for name in sorted(admitted_here):
        admission = admitted_here[name]
        agent = canon.agent(name)
        if agent is None or not NAME_RE.match(name):
            continue
        row = rows.get(name)
        known = registry.get(db, name)
        if known is not None and known["name"] != name:
            actions.append(SyncAction(name, "conflit", "primary name is another agent's alias",
                                      ["agent-alias-conflict"]))
            continue
        # L42 (0031) : noms globaux, premier déclarant. Une ligne gouvernée par
        # un AUTRE canon, et non libérée, n'est jamais reprise : constat limité
        # à cette fiche, aucune écriture.
        if row is not None and not mine(row) and row.get("canon_ref") \
                and not row.get("ephemeral") and not _released(row):
            message = ("agent %s already declared by %s (%s): card ignored, the first "
                       "canon that declares a name keeps it (to release it: remove its card "
                       "from its canon)" % (name, _owner_label(row.get("canon")),
                                          row.get("canon_ref")))
            findings.append(canon_mod.Finding(
                NAME_CONFLICT, canon_mod.ERROR, message, path=agent.fiche.path,
                member=agent.fiche.member, agent=name, untrusted=canon.untrusted))
            actions.append(SyncAction(name, "conflit", message, [NAME_CONFLICT]))
            continue
        # L165 (EP1) : sur un hôte candidat, le verdict de placement de CET
        # hôte est écrit même si la ligne est encore chez un autre candidat :
        # c'est lui qui autorisera la réclamation depuis ici.
        candidate = agent.releve == "election" and host in (agent.candidats or [])
        card_errors = list(block.by_agent.get(name, ()))
        # Hôte courant admis et différent de celui qu'on synchronise : on ne
        # touche à rien, l'exécution reste où elle est (0029). Une fiche en
        # erreur ferme néanmoins TOUS les candidats (L165), et son responsable
        # périmé est vidé même si la ligne est chez un autre détenteur.
        if row is not None and row.get("host") not in (None, host) \
                and row.get("host") in other_admitted.get(name, []):
            election_card = agent.releve == "election" and bool(card_errors)
            if candidate or election_card:
                if block.global_errors:
                    # Lecture locale incomplète : le sync du détenteur jugera.
                    pass
                elif card_errors:
                    _close_card_candidates(db, name, canon_key, card_errors)
                    if mine(row):
                        _clear_responsible(db, name, row.get("host") or host, owner)
                else:
                    _write_candidate(db, canon, agent, admission, host,
                                     canon_key=canon_key, forge=forge)
            actions.append(SyncAction(
                name, "laissé",
                "running on %s, an admitted host: \"canon sync\" does not move it"
                % row.get("host")))
            continue
        if row is None or row.get("host") in (None, host):
            # l'agent est (ou devient) de cet hôte : il reçoit les règles
            process_agents.add(name)
        reasons = block.reasons(name, [host])
        # L780 (R14) : une erreur PROPRE à l'agent vide son responsable — sa
        # fiche ne le confirme plus. Une erreur d'hôte ou sans sujet, elle,
        # ferme déjà toute réclamation par `canon_state` (fail closed) et ne
        # détruit pas le dernier responsable résolu (`keep last_good`) :
        # l'attribution d'un lot reste ouverte (L46), et la réparation du canon
        # rend les agents réclamables sans réécriture manuelle.
        if block.by_agent.get(name):
            responsible = None
        elif host_wide:
            responsible = row["responsible"] if row is not None else None
        else:
            responsible = canon.resolve_human(agent.responsible)
        verdict = placement_mod.evaluate(canon, name, host)
        # L31 (0029) : règle de visibilité, repliée dans le verdict de placement
        # (fail closed ; sans dépôt de mémoire, sans objet).
        visibility_ok, visibility_diag = visibility_mod.annotate(
            canon, agent, host, verdict, db=db, forge=forge)
        if candidate and card_errors:
            # L165 : le verdict de placement local ne valide pas la fiche.
            _close_card_candidates(db, name, canon_key, card_errors)
            verdict.ok = False
            verdict.diagnostic = _card_error_text(card_errors) + (
                "; " + verdict.diagnostic if verdict.diagnostic else "")
        # Dossier de travail (L31, 0029 ; L35 ; L101) : politique de l'hôte
        # d'abord (`workspaces.dirs[agent]`, `roots[projet]`, `root`), repli
        # transitoire sur le `cwd` d'une ancienne fiche Placement, noté ici.
        cwd, cwd_source = canon_mod.work_dir_for(canon.host(host), agent, admission)
        notes: list[str] = []
        if cwd_source == canon_mod.WORK_FROM_PLACEMENT:
            notes.append("cwd inherited from the Placement card (%s), to migrate to "
                         "the host's policy.workspaces.dirs / roots (legacy work_dirs / "
                         "work_roots still read)" % cwd)
        elif cwd is None:
            notes.append("no working directory: declare policy.workspaces.dirs / "
                         "roots / root in the host card")
        values = {
            "harness": agent.harness or "other",
            "host": host,
            "cwd": os.path.expanduser(cwd) if cwd else None,
            "model": agent.model,
            "budget_usd": agent.budget_usd_per_day,
            "responsible": responsible,
            # L243 (#435): every project of the card, the primary first; `team`
            # stays the primary one for the readers of a single project
            "team": agent.team,
            "projects": agent.effective_projects,
            "aliases": (list((row or {}).get("aliases") or [])
                        if any(r.startswith("agent-alias") for r in reasons)
                        else list(agent.aliases or [])),
            # L244 (#420): les rôles déclarés, vocable des fiches (L242c, #414),
            # pour que le garde SQL du verdict QA les lise au registre.
            "roles": agent.roles,
            "provider": agent.provider,
            "credential_mode": admission.credential_mode or agent.credential_mode,
            # `approve` n'entre jamais au registre (R8) ; la fiche est de toute
            # façon bloquée par l'erreur agent-approve-capability
            "capabilities": None if agent.capabilities is None else [
                c for c in agent.capabilities
                if c.strip().lower() not in canon_mod.FORBIDDEN_CAPABILITIES],
            "canon_ref": agent.fiche.ref,
            "ephemeral": False,
            # L31 (0028) : priorité de pause sous pression critique de l'hôte
            "priority": int(agent.priority or 0),
            # L31 (0029) : admission déclarative (hôtes résolus, étiquettes) et
            # dépôt de mémoire de la persona (règle de visibilité)
            "admitted_hosts": placement_mod.admitted_hosts(canon, admission),
            "admitted_tags": sorted(set(admission.tags())),
            "memory_repository": agent.memory_repository,
            "visibility_ok": visibility_ok,
            "visibility_diagnostic": visibility_diag,
            # C4 : admissibilité du placement sur CET hôte (politique de la fiche Host),
            # pour le profil de ces valeurs (`_write_declared` écrit le profil évalué ;
            # un harnais non déclaré, refusé par toute politique qui restreint les
            # harnais, est inscrit « other »)
            "placement_ok": verdict.ok,
            "placement_diagnostic": verdict.diagnostic,
            "placement_ref": verdict.ref,
            # L165 (EP1) : relève et hôtes candidats, déclaratifs comme le
            # placement ; un agent `hote` n'a pas de liste (NULL).
            "releve": agent.releve,
            "candidats": (list(agent.candidats) if agent.candidats is not None
                          else None),
            # #3185 : les projets dont l'agent relit les lots, hors son équipe
            "relit": list(agent.relit) if agent.relit is not None else None,
            # L42 (0031) : canon déclarant (NULL pour le canon par défaut)
            "canon": None if canon.is_default else ident,
            # Reject writes based on a pre-handoff read, including after the
            # handoff is terminal. A missing-row read cannot overwrite a row
            # inserted concurrently by another synchronizer.
            "_expected_lease_epoch": row["lease_epoch"] if row is not None else -1,
        }
        if row is not None and _same(row, values):
            action = SyncAction(name, "inchangé")
        else:
            if not _write_declared(db, name, values):
                # A claim or handoff advanced the epoch after our read.
                # Do not replay old placement values against the new epoch,
                # report success, or reopen admission on an unapplied policy.
                message = ("policy update for %s deferred: placement or lease changed "
                           "during synchronization; retry canon sync" % name)
                findings.append(canon_mod.Finding(
                    SYNC_INCOMPLETE, canon_mod.ERROR, message,
                    path=agent.fiche.path, member=agent.fiche.member,
                    host=host, untrusted=canon.untrusted))
                status, diagnostic = assess(canon, host, findings)
                record_state(db, host, status, diagnostic, canon, key=key,
                             allow_rewind=allow_rewind)
                actions.append(SyncAction(name, "différé", message, [SYNC_INCOMPLETE]))
                continue
            action = SyncAction(name, "créé" if row is None else "mis à jour")
        if candidate and not card_errors:
            _write_candidate(db, canon, agent, admission, host,
                             canon_key=canon_key, forge=forge)
        action.placement = verdict
        if reasons:
            action.blocked = reasons
            notes.insert(0, "not claimable: blocking canon error")
        if not verdict.ok:
            notes.insert(0, "not claimable: placement refused — %s" % verdict.diagnostic)
        action.detail = "; ".join(notes)
        actions.append(action)
        if row is not None and row.get("status") == "stopped" \
                and (row.get("status_text") or "").startswith(STOP_MARK):
            if storage.of(db).canon.revive(name, REVIVED_TEXT, STOP_MARK):
                actions.append(SyncAction(name, "réintégré", "stopped by sync, back in the canon"))
        elif row is not None and (row.get("status_text") or "").startswith(PENDING_MARK):
            storage.of(db).canon.clear_pending_stop(name, PENDING_MARK)

    for name, row in sorted(rows.items()):
        if name in admitted_here:
            continue
        target = row.get("host") or host
        cross = target != host
        if cross and name not in removed_candidates:
            continue
        if row.get("ephemeral") or not row.get("canon_ref"):
            continue  # éphémère, ou jamais venu du canon : sync ne le gouverne pas
        if not mine(row):
            continue  # L42 : ligne d'un autre canon, jamais touchée
        member = _member_of(row.get("canon_ref"))
        if cross and (block.global_errors or member not in loaded):
            # Lecture locale incomplète : le sync du détenteur jugera, on ne
            # touche ni son responsable ni son placement ni son bail.
            continue
        unverifiable = ("member %s not read" % member if member not in loaded
                        else "invalid canon" if block.global_errors else "")
        if unverifiable:
            # Fiche invérifiable : ni arrêté (le retrait n'est pas constaté), ni
            # réclamable (son responsable n'est plus confirmé par le canon, son
            # placement non plus). L780 : seul un membre NON LU, dont la fiche
            # n'est pas confirmée, vide le responsable ; un canon globalement
            # invalide garde le dernier responsable résolu (keep last_good) —
            # `canon_state` ferme déjà la réclamation.
            if member not in loaded:
                _clear_responsible(db, name, target, owner)
            verdict = placement_mod.Verdict(
                name, target, False, "unverifiable placement: %s" % unverifiable,
                row.get("placement_ref"))
            _set_placement(db, name, target, verdict)
            actions.append(SyncAction(
                name, "laissé", "%s: removal deferred, not claimable" % unverifiable,
                block.reasons(name) if block.global_errors else [], verdict))
            continue
        cibles = other_admitted.get(name)
        if cibles and canon.agent(name) is not None:
            target = cibles[0]
            # L'hôte courant n'est plus admis, mais un autre hôte l'est : le
            # canon le déplace là (jamais hors d'un hôte admis).
            verdict = placement_mod.Verdict(
                name, target, None,
                "moved from %s: admitted host %s, to be evaluated by \"ameesh canon sync\" on %s"
                % (host, target, target), row.get("placement_ref"))
            storage.of(db).canon.move_host(
                name, host, target=target, canon_ref=canon.agent(name).fiche.ref,
                diagnostic=verdict.diagnostic, ref=verdict.ref)
            actions.append(SyncAction(
                name, "déplacé",
                "admitted on %s: not claimable until that host is synced" % target,
                placement=verdict))
            continue
        why = "no longer admitted" if canon.agent(name) is not None else "sheet missing"
        # Sans admission sur cet hôte, plus réclamable ici, quel que soit son
        # statut (fin de tour comprise) : écrit avant l'arrêt, colonnes
        # déclaratives seulement. Un rôle élu retiré du canon ne garde pas de
        # candidats.
        storage.of(db).canon.set_election(name, "hote", None, canon=canon_key)
        verdict = placement_mod.evaluate(canon, name, target)
        _set_placement(db, name, target, verdict)
        if cross:
            # Fiche absente constatée depuis un autre candidat : le
            # responsable n'est plus confirmé, et le retrait n'est appliqué
            # que si le bail n'a pas changé depuis la lecture (guard epoch).
            _clear_responsible(db, name, target, owner)
        stopped = storage.of(db).canon.stop_removed(
            name, target, pending_text="%s (%s)" % (PENDING_MARK, why),
            stop_text="%s (%s)" % (STOP_MARK, why), owner=owner,
            expected_lease_epoch=row.get("lease_epoch") if cross else None)
        if stopped is None:
            continue
        if stopped["status"] == "stopped":
            actions.append(SyncAction(name, "arrêté", why, placement=verdict))
        else:
            actions.append(SyncAction(name, "arrêt demandé",
                                      "%s: turn in progress, nothing is killed" % why,
                                      placement=verdict))
    for name, row in sorted(rows.items()):
        if canon.is_default and row.get("host") == host and not row.get("canon_ref") \
                and not row.get("ephemeral") and name not in admitted_here:
            actions.append(SyncAction(name, "hors canon",
                                      "registered by hand: not governed by sync"))
    _inherit_placements(db, host, canon)
    packages = sync_packages(db, canon, findings, ident=ident)
    findings.extend(packages.findings)
    # #1616 : compilation des paramètres mécaniques du canon appliqué. Un
    # document absent ou mal formé est un CONSTAT, jamais une fermeture du
    # canon : les consommateurs raccordés, eux, refusent (lecteur strict).
    machine = sync_mechanics(canon)
    processes = ProcessSync()
    if status == CANON_OK:
        # les conflits de noms (L42) figurent au diagnostic, sans fermer le canon
        status, diagnostic = assess(canon, host, findings)
        if status == CANON_OK:
            # #538 : les règles de travail modifiées partent aux agents de cet
            # hôte AVANT que l'état n'avance (le repère de comparaison est
            # l'ancien commit, encore dans canon_state) ; #793 : seulement si
            # ce commit est enregistré `ok`, et par une seule synchronisation.
            processes = sync_processes(db, canon, host, sorted(process_agents))
        record_state(db, host, status, diagnostic, canon, key=key,
                     allow_rewind=allow_rewind)
    # registre des authentificateurs (§8.2) : copie de travail des fiches
    # Member, quel que soit l'état du canon pour cet hôte (ses propres règles).
    # L44 (0031) : CHAQUE canon synchronise les siens, dans son registre et son
    # journal (clé `key`, celle de son état) ; un refus ou une erreur de l'un
    # n'est inscrit que dans SON état et ne touche pas le registre de l'autre.
    authenticators = sync_authenticators(db, canon, trusted_ref=trusted_ref,
                                         bootstrap_ref=bootstrap_ref, host=host, key=key,
                                         checkout_block=checkout_block)
    # jamais silencieux : diagnostic durable (canon_state) et fil
    record_authenticators(db, host, authenticators, key)
    return SyncReport(host=host, source=canon.source_label(), untrusted=canon.untrusted,
                      actions=actions, findings=findings, status=status,
                      diagnostic=diagnostic, authenticators=authenticators,
                      packages=packages, processes=processes, mechanics=machine)


# --------------------------------------------------------------------------
# plan de travail (L29)
# --------------------------------------------------------------------------

@dataclass
class PackageSync:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    #: fiches en erreur : non écrites (la copie précédente, s'il y en a une, reste)
    skipped: list[str] = field(default_factory=list)
    relinked: int = 0
    #: L42 (0031) : identifiants déjà déclarés par un autre canon (non écrits)
    conflicts: list[str] = field(default_factory=list)
    #: constats `canon-name-conflict` de ces fiches (ne bloquent aucun agent)
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {"created": self.created, "updated": self.updated,
               "unchanged": self.unchanged, "retired": self.retired,
               "skipped": self.skipped, "relinked_work_items": self.relinked}
        if self.conflicts:
            out["conflicts"] = self.conflicts
        return out


_PACKAGE_KEYS = ("kind", "title", "parent", "responsible", "team", "scope", "status",
                 "canon_ref", "canon",
                 # L96 : dates déclarées au canon
                 "start_on", "end_on", "delivery_on")


def sync_packages(db: Db, canon: Canon, findings: list[Finding], *,
                  ident: str | None = None) -> PackageSync:
    """Recopie les fiches WorkPackage dans `work_packages` (une transaction).

    L42 (0031) : seuls les paquets de CE canon (colonne `canon`, NULL = canon
    par défaut) sont mis à jour ou retirés ; un identifiant présent d'un autre
    canon n'est jamais repris (constat `canon-name-conflict`), un identifiant
    retiré (`present` faux) peut l'être."""
    done = PackageSync()
    ident = ident or canon.id
    bad = {f.package for f in canon_mod.errors(findings) if f.package}
    loaded = canon.loaded_members()

    def mine(row: dict) -> bool:
        value = row.get("canon")
        return (canon.is_default and not value) or value == ident

    with db.transaction() as tx:
        st = storage.of(tx)
        # #802/#831 : au démarrage d'un hôte, N exécutants synchronisent le même
        # canon en parallèle. Sans sérialisation, chacun lit le même état
        # initial (`before`), rejoue les mêmes upserts et attend les verrous de
        # ligne du premier : la file de verrous dépasse statement_timeout, le
        # sync échoue et la porte d'hôte reste fermée (« canon not read in
        # full, no claim »). Le verrou est pris AVANT la lecture
        # (`packages.lock_sync`) : une fois le premier sync commité, les
        # suivants relisent l'état à jour et n'écrivent rien. Lié à la
        # transaction, il est relâché au commit comme au rollback ; un seul
        # verrou par schéma, tous canons confondus — les identifiants de
        # paquets sont globaux (le premier canon qui en déclare un le garde),
        # et la bascule du canon par défaut écrit la même colonne.
        st.packages.lock_sync()
        before = {row["id"]: row for row in st.packages.all(include_absent=True)}
        seen: set[str] = set()
        for package in canon.packages:
            seen.add(package.id)
            if package.id in bad:
                done.skipped.append(package.id)
                continue
            old = before.get(package.id)
            if old is not None and old.get("present") and not mine(old):
                message = ("WorkPackage %s already declared by %s (%s): card ignored, the "
                           "first canon that declares an identifier keeps it"
                           % (package.id, _owner_label(old.get("canon")),
                              old.get("canon_ref")))
                done.conflicts.append(package.id)
                done.findings.append(canon_mod.Finding(
                    NAME_CONFLICT, canon_mod.ERROR, message, path=package.fiche.path,
                    member=package.fiche.member, package=package.id,
                    untrusted=canon.untrusted))
                continue
            row = {"id": package.id, "kind": package.kind, "title": package.title,
                   "parent": package.parent, "responsible": package.responsible,
                   "team": package.team, "scope": package.scope, "status": package.status,
                   "canon_ref": package.fiche.ref,
                   "canon": None if canon.is_default else ident,
                   "start_on": package.start, "end_on": package.end,
                   "delivery_on": package.delivery}
            if old is not None and old.get("present") and all(
                    (old.get(k) or None) == (row.get(k) or None) for k in _PACKAGE_KEYS):
                done.unchanged.append(package.id)
                continue
            st.packages.upsert(row)
            (done.created if old is None else done.updated).append(package.id)
        gone = [ident_ for ident_, row in before.items()
                if row.get("present") and ident_ not in seen and mine(row)
                and _member_of(row.get("canon_ref")) in loaded]
        st.packages.retire(gone)
        done.retired = sorted(gone)
        done.relinked = st.work.refresh_package_parents()
    return done


# --------------------------------------------------------------------------
# paramètres mécaniques (#1616)
# --------------------------------------------------------------------------

@dataclass
class MechanicsSync:
    """#1616 : le bloc mécanique (`processus/mecanique.yaml`) compilé au commit
    appliqué. Constat : un document absent ou invalide ne ferme pas le canon
    (les consommateurs raccordés refusent, eux, par [`mechanics.load`])."""

    #: `ok` (compilé), `absent` (pas de document au commit), `invalid`
    #: (schéma ou règle refusés), `untrusted` (pas de commit exact)
    status: str = "absent"
    schema: str = ""
    timezone: str = ""
    cutoffs: list[str] = field(default_factory=list)
    #: cadence des paquets horaires (#1757) ; liste vide = aucune cadence déclarée
    packets: list[str] = field(default_factory=list)
    max_in_build: int = 0
    ci: dict = field(default_factory=dict)
    reviews: dict = field(default_factory=dict)
    projects: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        out = {"status": self.status, "schema": self.schema,
               "timezone": self.timezone, "cutoffs": list(self.cutoffs),
               "packets": list(self.packets),
               "max_in_build": self.max_in_build, "ci": dict(self.ci),
               "reviews": dict(self.reviews), "projects": list(self.projects)}
        if self.error:
            out["error"] = _clip(self.error)
        return out


def sync_mechanics(canon: Canon) -> MechanicsSync:
    """Compile `processus/mecanique.yaml` au commit exact du canon appliqué."""
    done = MechanicsSync()
    if not canon.sources:
        done.status = "untrusted"
        done.error = "no canon source"
        return done
    source = canon.sources[0]
    if source.mode != "git" or not source.commit:
        done.status = "untrusted"
        done.error = "no exact canon commit applied"
        return done
    if source.prefix:
        # le lecteur strict lit `processus/mecanique.yaml` à la racine Git du
        # canon : sous un sous-répertoire, il lirait un autre fichier que celui
        # du canon appliqué — refus explicite, jamais un document voisin
        done.status = "invalid"
        done.error = ("canon under a sub-directory (%s): the mechanics are read "
                      "at the Git root only" % source.prefix)
        return done
    try:
        document = mechanics.load_global(source.repo, source.commit)
    except mechanics.MechanicsAbsent as exc:
        done.status = "absent"
        done.error = str(exc)
        return done
    except mechanics.MechanicsError as exc:
        done.status = "invalid"
        done.error = str(exc)
        return done
    done.status = "ok"
    done.schema = document.schema
    done.timezone = document.timezone
    done.cutoffs = list(document.cutoffs)
    done.packets = list(document.packet_cutoffs)
    done.max_in_build = document.max_in_build
    done.ci = {"packet": document.ci_packet,
               "incident_label": document.ci_incident_label}
    done.reviews = {"families": list(document.families),
                    "exclude_author_family": document.exclude_author_family,
                    "distinct_families": document.distinct_families}
    done.projects = sorted(item.name for item in document.overrides)
    return done


# --------------------------------------------------------------------------
# règles de travail (#538)
# --------------------------------------------------------------------------

@dataclass
class ProcessSync:
    """#538 : fiches Process modifiées depuis le dernier commit appliqué,
    poussées en message `event` aux agents de l'hôte — leur prochain tour les
    lit, sans réveiller personne."""

    #: titres des fiches poussées
    changed: list[str] = field(default_factory=list)
    #: événements déposés (une fiche × ses destinataires)
    sent: int = 0
    #: destinataires retirés au dépôt, ignorés sans annuler la synchronisation
    skipped_retired: list[str] = field(default_factory=list)
    #: diff git illisible : aucune poussée, les règles restent lues par le brief
    error: str = ""

    def to_dict(self) -> dict:
        out = {"changed": self.changed, "sent": self.sent,
               "skipped_retired": self.skipped_retired}
        if self.error:
            out["error"] = self.error
        return out


def sync_processes(db: Db, canon: Canon, host: str, agents: list[str]) -> ProcessSync:
    """#538 : pousse aux agents de `host` les fiches Process modifiées depuis
    le dernier commit poussé (`canon_state.process_commit` ; au premier envoi,
    le dernier état connu `last_good_commit`).

    La première synchronisation ne pousse rien : elle pose le repère. Un diff
    illisible ne pousse rien non plus (`error`) : mieux vaut manquer un
    rappel que pousser un texte qu'on n'a pas su relire — la règle, elle, est
    déjà dans le brief de la prochaine session.

    #793 : les synchronisations concurrentes d'un hôte lisent le même ancien
    commit ; seule celle qui l'avance (`claim_commit`, dans la transaction
    des envois) pousse, les autres ne poussent rien. Le repère est indépendant
    de `record_state`, qu'un sync ancien peut écrire après un sync récent ;
    un commit antérieur au repère ne pousse jamais des règles périmées.

    #1464 : un repère qui n'appartient à aucune des deux histoires (canon
    réécrit, repère d'un autre canon) ne peut plus produire de diff ; il est
    signalé (`error`) au lieu de laisser les envois s'arrêter en silence."""
    done = ProcessSync()
    if not canon.sources or not canon.processes or not agents:
        return done
    source = canon.sources[0]
    if source.mode != "git" or not source.commit:
        return done
    previous = state(db, host, canon) or {}
    old = previous.get("process_commit") or previous.get("last_good_commit") or ""
    if not old or old == source.commit:
        return done
    try:
        if not canon_mod.git_contains(source.repo, old, source.commit):
            if canon_mod.git_contains(source.repo, source.commit, old):
                # sync ancien : un sync plus récent a déjà poussé au-delà de ce
                # commit — rien à pousser, rien à signaler, repère conservé
                return done
            # #1464 (N1 #793) : repère figé — l'historique du canon a été réécrit
            # (ou le repère vient d'un autre dépôt) : le diff `old..source.commit`
            # est impossible, et sans ré-ancrage du repère `process_commit` les
            # envois de règles ne repartiront JAMAIS. On le dit (`error`) au lieu
            # de perdre les envois en silence ; les règles, elles, restent dans le
            # brief de la prochaine session.
            done.error = (
                "process_commit %s is not in the history of %s: the canon history "
                "was rewritten (or this marker comes from another canon) — the "
                "changed Process rules are no longer pushed on this host until "
                "the push marker is re-anchored" % (old[:12], source.commit[:12]))
            return done
        # #538 : la comparaison porte sur la RÉFÉRENCE APPROUVÉE (`source.commit`),
        # pas sur HEAD ni l'arbre de travail : si la référence avance sans que le
        # checkout suive, le changement est quand même poussé (sinon le point de
        # reprise avancerait en silence, et la règle serait perdue).
        touched = set(canon_mod.git_changed_paths_between(source.repo, old,
                                                          source.commit))
    except canon_mod.GitError as exc:
        done.error = str(exc)
        return done
    changed = [process for process in canon.processes
               if process.valid and process.fiche.member == source.member
               and process.fiche.path in touched]
    if not changed:
        return done
    from . import mail as mail_mod
    from . import work as work_mod
    with db.transaction() as tx:
        if not storage.of(tx).canon.claim_commit(host, old, source.commit,
                                                 state_key(db, canon, host)):
            return done
        for process in changed:
            texte = ("Règles de travail du canon : %s\n\n%s"
                     % (process.title, (process.body or "").strip())).strip()
            for name in sorted(set(agents)):
                if name in done.skipped_retired:
                    continue
                try:
                    mail_mod.send(tx, work_mod.SYSTEM_SENDER, name, texte, host=host,
                                  kind="event",
                                  payload={"event": "canon-process",
                                           "path": process.fiche.path},
                                  thread=False)
                except storage.RecipientRetired:
                    # #5080 : le retrait peut précéder celui de la fiche canon.
                    # Le dépôt refuse sans erreur SQL ; la transaction reste
                    # utilisable. Les autres erreurs doivent toujours remonter.
                    done.skipped_retired.append(name)
                    continue
                done.sent += 1
            done.changed.append(process.title)
    return done


# --------------------------------------------------------------------------
# agents éphémères (R14)
# --------------------------------------------------------------------------

def parse_ttl(text: str | None) -> float:
    """« 30m », « 2h », « 1d », « 3600 » → secondes, borné à 7 jours. Obligatoire."""
    if not text:
        raise SpawnError("deadline required: --ttl 30m, 2h, 1d…")
    match = _TTL_RE.match(text)
    if not match:
        raise SpawnError("unreadable deadline: %r (expected 30m, 2h, 1d or seconds)" % text)
    seconds = float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[
        match.group(2).lower()]
    if seconds <= 0:
        raise SpawnError("zero deadline")
    if seconds > MAX_EPHEMERAL_TTL:
        raise SpawnError("deadline too far for an ephemeral (maximum %d days)"
                         % (MAX_EPHEMERAL_TTL // 86400))
    return seconds


def spawn(db: Db, name: str, creator: str, ttl_seconds: float, *,
          cwd: str | None = None, canon: "Canon | list[Canon] | None" = None) -> dict:
    """Crée un agent éphémère : responsable copié du créateur, capacités ⊆ {read, propose}.

    L'insertion est atomique (INSERT … SELECT sur la ligne du créateur) : le
    responsable copié est celui du créateur au moment de la création. Un
    créateur éphémère transmet aussi son échéance (l'enfant ne lui survit pas).
    Le verdict de placement (C4) est recopié lui aussi, avec le profil évalué
    du créateur : l'éphémère tourne sur le même hôte, avec le même harnais, le
    même fournisseur, le même modèle et le même mode — mais un verdict admis
    n'est hérité que si ce profil (celui du créateur au moment de la
    création) est encore le profil évalué ; sinon l'éphémère naît refusé
    (« profil divergé »). `canon sync` le rafraîchit ensuite depuis le
    créateur racine.
    """
    if not NAME_RE.match(name or ""):
        raise SpawnError("invalid agent name: %r" % (name,))
    if not NAME_RE.match(creator or ""):
        raise SpawnError("invalid creator: %r" % (creator,))
    if name == creator:
        raise SpawnError("an agent cannot create itself")
    # L42 (0031) : le nom ne doit être déclaré par AUCUN des canons configurés
    for one in (canon if isinstance(canon, (list, tuple)) else [canon]):
        if one is not None and one.readable and one.agent(name) is not None:
            raise SpawnError("%s has a card in the canon%s: it is not an ephemeral"
                             % (name, "" if one.is_default else " %s" % one.id))
    parent = storage.of(db).ephemerals.creator(creator)
    if parent is None:
        raise SpawnError("creator unknown to the registry: %s" % creator)
    if not (parent.get("responsible") or "").strip():
        raise SpawnError("creator %s has no resolved responsible human (R14): "
                         "it cannot pass on responsibility" % creator)
    if not parent.get("alive"):
        raise SpawnError("creator %s is an expired ephemeral" % creator)
    if parent.get("status") == "stopped":
        raise SpawnError("creator %s is stopped" % creator)
    created = storage.of(db).ephemerals.create(
        name, creator, cwd=os.path.abspath(os.path.expanduser(cwd)) if cwd else None,
        ttl_seconds=ttl_seconds)
    if created is None:
        if storage.of(db).ephemerals.exists(name):
            raise SpawnError("an agent %s already exists" % name)
        raise SpawnError("creator %s changed during creation: retry" % creator)
    return created
