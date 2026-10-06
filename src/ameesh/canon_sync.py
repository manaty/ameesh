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

Le SQL est dans le stockage (`storage.of(db).canon`, `.ephemerals`,
`.authenticators`, spec §10) ; la transaction du registre des
authentificateurs, verrou compris, y est UNE opération
(`authenticators.under_registry_lock`).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from . import canon as canon_mod
from . import fil
from . import placement as placement_mod
from . import receipts, registry, storage
from . import visibility as visibility_mod
from .canon import Canon, Finding
from .config import NAME_RE
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
               "placement_ok", "placement_diagnostic", "placement_ref")

#: statuts de `canon_state` (0008)
CANON_OK = "ok"
CANON_INVALID = "invalid"
CANON_UNREADABLE = "unreadable"
_DIAGNOSTIC_MAX = 4000


class SpawnError(ValueError):
    pass


class CanonUnreadable(canon_mod.CanonError):
    """Canon illisible : l'état `unreadable` est enregistré, rien d'autre."""

    def __init__(self, host: str, diagnostic: str, findings: list[Finding]):
        super().__init__("canon illisible, registre inchangé : %s" % diagnostic)
        self.host = host
        self.diagnostic = diagnostic
        self.findings = findings


@dataclass
class SyncAction:
    agent: str
    action: str     # créé | mis à jour | inchangé | arrêté | arrêt demandé | réintégré | déplacé | laissé
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
            text = "REFUSÉ, registre inchangé — %s" % self.reason
        elif self.errors:
            frozen = list((self.result or {}).get("frozen") or [])
            text = "%d erreur(s)%s : %s" % (
                len(self.errors), " (GELÉS : %s)" % ", ".join(frozen) if frozen else "",
                " ; ".join(self.errors))
        elif self.status == AUTH_SKIPPED:
            text = self.reason
        else:
            text = " ; ".join(list(self.notes) + (
                ["canon lu en partie, aucune révocation pour absence : %s"
                 % ", ".join(self.partial)] if self.partial else []))
        return _clip(text)

    def to_dict(self) -> dict:
        out: dict = {"status": self.status, "reason": self.reason,
                     "errors": self.errors, "partial": self.partial,
                     "trust": self.trust, "branch": self.branch, "notes": self.notes,
                     "state": self.state}
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
                "packages": self.packages.to_dict() if self.packages is not None else None}


# --------------------------------------------------------------------------
# état du canon par hôte (canon_state, §4.1)
# --------------------------------------------------------------------------

def _clip(text: str) -> str:
    return text if len(text) <= _DIAGNOSTIC_MAX else text[:_DIAGNOSTIC_MAX - 1] + "…"


def _describe(findings: list[Finding]) -> str:
    return _clip(" ; ".join("%s (%s) : %s" % (f.code, f.where(), f.message) for f in findings))


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
        return CANON_UNREADABLE, _describe(reasons) if reasons else "aucune source lue"
    if findings is None:
        findings = canon_mod.validate(canon)
    block = canon_mod.blocking(findings)
    host_wide = block.global_errors + list(block.by_host.get(host, []))
    if host_wide:
        return CANON_INVALID, _describe(host_wide)
    others = canon_mod.errors(findings)
    if others:
        return CANON_OK, ("erreurs propres à des agents (non réclamables) ou au plan : "
                          + _describe(others))
    return CANON_OK, ""


def record_state(db: Db, host: str, status: str, diagnostic: str,
                 canon: Canon | None = None) -> None:
    """Écrit l'état du canon de `host` ; le dernier commit valide ne suit que `ok`."""
    commit = None
    if status == CANON_OK and canon is not None and canon.sources:
        commit = canon.sources[0].commit or None
    storage.of(db).canon.record_state(
        host, status, root=canon.root if canon is not None else "",
        source=canon.source_label() if canon is not None and canon.sources else "",
        commit=commit, good=status == CANON_OK, diagnostic=diagnostic)


def state(db: Db, host: str) -> dict | None:
    """Dernier état enregistré du canon de `host`, ou None."""
    return storage.of(db).canon.state(host)


def record_authenticators(db: Db, host: str, done: AuthenticatorSync) -> None:
    """Inscrit l'issue de la synchronisation du registre des authentificateurs.

    Diagnostic durable : `canon_state.auth_status` / `auth_diagnostic` de
    `host` (la ligne de l'hôte, écrite juste avant par `record_state`). Une
    erreur rend cette partie non-ok (`error`) sans toucher au statut du canon
    pour les agents (`status`, réclamation). Fil : une erreur NOUVELLE (statut
    ou diagnostic différent du précédent) est écrite dans le fil du projet,
    et le retour à la normale aussi — un exécuteur qui resynchronise
    périodiquement ne répète pas la même entrée, mais rien n'est silencieux.
    """
    status, diagnostic = done.state, done.diagnostic()
    previous = storage.of(db).canon.record_auth_state(host, status, diagnostic)
    before = (previous.get("auth_status"), previous.get("auth_diagnostic") or "") \
        if previous is not None else (None, "")
    if status == AUTH_STATE_ERROR and before != (status, diagnostic):
        _post_authenticators(db, host, status, (
            "canon sync sur %s : registre des authentificateurs EN ERREUR — %s. "
            "Détail : ameesh canon sync --host %s ; état durable : ameesh canon check --host %s."
            % (host, diagnostic or "?", host, host)), done)
    elif status != AUTH_STATE_ERROR and before[0] == AUTH_STATE_ERROR:
        _post_authenticators(db, host, status, (
            "canon sync sur %s : registre des authentificateurs de nouveau sans erreur (%s)%s."
            % (host, status, " — %s" % diagnostic if diagnostic else "")), done)


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
                         "branch": done.branch, "trust": done.trust})
    except Exception as exc:  # noqa: BLE001 - fil.record ne lève pas ; prudence
        fil.warn("%s (entrée du fil non écrite : %s)" % (text, " ".join(str(exc).split())[:200]))


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
    TRUST_CONFIG: "configuration de l'hôte",
    TRUST_APPLIED: "manifeste du dernier commit appliqué",
    TRUST_BOOTSTRAP: "amorçage --bootstrap-ref",
}
BOOTSTRAP_REFUSED = (
    "premier amorçage du registre des authentificateurs refusé : aucune branche "
    "canonique de confiance (ni AMEESH_CANON_REF / canon_ref dans la configuration de "
    "l'hôte, ni commit déjà appliqué) ; le commit lu ne décide jamais lui-même de ce "
    "qui est canonique — registre inchangé, « ameesh canon sync --bootstrap-ref "
    "<branche> » (journalisé)")


class _Refused(Exception):
    """Contrôle refusé sous le verrou : rien n'est écrit."""


class _Concurrent(Exception):
    """Le journal a bougé pendant la transaction : tout est annulé."""


def configured_ref(db: Db) -> str:
    """Branche canonique de confiance de la configuration de l'hôte
    (AMEESH_CANON_REF / `canon_ref`), lue sur la connexion (`db.cfg`)."""
    return str(getattr(getattr(db, "cfg", None), "canon_ref", "") or "")


def last_applied(db: Db) -> dict | None:
    """Dernière synchronisation appliquée (journal `authenticator_syncs`), ou None."""
    row = storage.of(db).authenticators.last_sync()
    if row is None:
        return None
    commits = row.get("commits")
    if isinstance(commits, str):
        commits = json.loads(commits)
    row["commits"] = {str(k): str(v) for k, v in (commits or {}).items()} \
        if isinstance(commits, dict) else {}
    row["id"] = int(row["id"])
    return row


def _applied_branch(root: canon_mod.Source, last: dict) -> str:
    """La branche canonique selon le manifeste du DERNIER commit appliqué
    (`ref` de son membre racine), sinon celle contre laquelle ce commit a été
    vérifié. Un changement de branche n'y entre qu'une fois fusionné."""
    commit = last["root_commit"]
    if not canon_mod.git_commit(root.repo, commit):
        raise _Refused(
            "canon en retard sur le registre des authentificateurs : le dernier commit "
            "appliqué %s est inconnu de ce clone — registre inchangé, ameesh canon sync "
            "--fetch" % commit[:12])
    path = (root.prefix + "/" if root.prefix else "") + "federation.yaml"
    raw = canon_mod.git_read(root.repo, commit, path)
    if raw is None:
        return last["branch"]
    try:
        manifest = canon_mod.load_yaml(raw.decode("utf-8"))
    except (UnicodeDecodeError, canon_mod.YamlError) as exc:
        raise _Refused("federation.yaml illisible au dernier commit appliqué %s (%s) : "
                       "branche canonique inconnue, registre inchangé" % (commit[:12], exc))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("members") or [], list):
        raise _Refused("federation.yaml invalide au dernier commit appliqué %s : branche "
                       "canonique inconnue, registre inchangé" % commit[:12])
    root_id = str(manifest.get("root") or "canon")
    entry = next((m for m in manifest.get("members") or []
                  if isinstance(m, dict) and str(m.get("id")) == root_id), None)
    ref = str((entry or {}).get("ref") or "")
    if not ref:
        return last["branch"]
    branch = canon_mod.remote_branch(root.repo, ref)
    if branch is None:
        raise _Refused("ref %r du membre racine %s (manifeste du dernier commit appliqué "
                       "%s) : pas une branche, registre inchangé" % (ref, root_id, commit[:12]))
    return branch


def _trusted_branch(root: canon_mod.Source, last: dict | None, trusted_ref: str,
                    bootstrap_ref: str) -> tuple[str, str, list[str]]:
    """(branche canonique de confiance, origine, remarques) — jamais tirée du
    commit lu : configuration de l'hôte, sinon manifeste du dernier commit
    appliqué, sinon amorçage explicite ; sinon refus."""
    notes: list[str] = []
    if trusted_ref:
        branch = canon_mod.remote_branch(root.repo, trusted_ref)
        if branch is None:
            raise _Refused("AMEESH_CANON_REF %r (configuration de l'hôte) ne désigne pas une "
                           "branche : branche canonique de confiance inconnue, registre "
                           "inchangé" % trusted_ref)
        trust = TRUST_CONFIG
    elif last is not None:
        branch, trust = _applied_branch(root, last), TRUST_APPLIED
    elif bootstrap_ref:
        branch = canon_mod.remote_branch(root.repo, bootstrap_ref)
        if branch is None:
            raise _Refused("--bootstrap-ref %r ne désigne pas une branche : registre "
                           "inchangé" % bootstrap_ref)
        trust = TRUST_BOOTSTRAP
        notes.append("AMORÇAGE du registre des authentificateurs : branche canonique de "
                     "confiance %s fixée par --bootstrap-ref (journalisé)" % branch)
    else:
        raise _Refused(BOOTSTRAP_REFUSED)
    if bootstrap_ref and trust != TRUST_BOOTSTRAP:
        notes.append("--bootstrap-ref %s ignoré : %s" % (
            bootstrap_ref, "registre déjà amorcé" if trust == TRUST_APPLIED
            else "la configuration de l'hôte fixe la branche canonique"))
    return branch, trust, notes


def _check_on_branch(source: canon_mod.Source, branch: str, why: str) -> None:
    """Le commit lu est-il atteignable depuis la branche canonique de confiance ?"""
    head = canon_mod.git_commit(source.repo, "refs/remotes/" + branch)
    if not head:
        raise _Refused("%s : branche canonique %s (%s) introuvable dans %s (git fetch ?) — "
                       "registre des authentificateurs inchangé"
                       % (source.member, branch, why, source.repo))
    if not canon_mod.git_contains(source.repo, source.commit, head):
        raise _Refused("%s @ %s : commit absent de la branche canonique %s (%s, %s) — "
                       "révision non canonique, registre des authentificateurs inchangé"
                       % (source.member, source.commit[:12], branch, head[:12], why))


def _check_descends(source: canon_mod.Source, floor: str | None) -> None:
    """Monotonie : jamais d'écriture d'un commit qui ne descend pas du dernier appliqué."""
    if floor and not canon_mod.git_contains(source.repo, floor, source.commit):
        raise _Refused(
            "%s @ %s ne descend pas du dernier commit appliqué %s : canon en retard "
            "(ameesh canon sync --fetch) ou historique réécrit — jamais de retour en "
            "arrière, registre des authentificateurs inchangé"
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
    branch, trust, notes = _trusted_branch(root, last, trusted_ref, bootstrap_ref)
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
            raise _Refused("%s : ref %r du manifeste, pas une branche — registre inchangé"
                           % (source.member, ref))
        _check_on_branch(source, member_branch, "manifeste du commit racine %s"
                         % root.commit[:12])
        if last is not None:
            _check_descends(source, last["commits"].get(source.member))
    return branch, trust, notes


def _record(db: Db, last: dict | None, canon: Canon, branch: str, trust: str, host: str,
            result: dict) -> None:
    """Journalise la synchronisation appliquée (dernier commit appliqué), en
    vérifiant que le journal n'a pas bougé depuis sa lecture sous le verrou."""
    root = canon.sources[0]
    commits = dict(last["commits"]) if last is not None else {}
    commits.update({s.member: s.commit for s in canon.sources if s.mode == "git"})
    changed = any(result.get(key) for key in ("added", "updated", "revoked"))
    expected = last["id"] if last is not None else None
    if last is not None and not changed and (
            last["root_commit"], last["commits"], last["branch"]) == (root.commit, commits,
                                                                      branch):
        head = storage.of(db).authenticators.journal_head()
        if head is None or head != expected:
            raise _Concurrent("journal des synchronisations modifié pendant la transaction")
        return
    summary = {key: len(result.get(key) or []) for key in ("added", "updated", "revoked",
                                                           "frozen", "errors")}
    appended = storage.of(db).authenticators.append_sync(
        root_member=root.member, root_commit=root.commit,
        commits_json=json.dumps(commits, sort_keys=True), branch=branch, trust=trust,
        host=host or "", summary_json=json.dumps(summary, sort_keys=True),
        expected=expected)
    if not appended:
        raise _Concurrent("journal des synchronisations modifié pendant la transaction")


def _lag(db: Db, canon: Canon) -> tuple[list[str], set[str]]:
    """Le canon lu est-il en retard sur ce que le registre a déjà appliqué ?

    Chaque ligne écrite par une synchronisation porte le commit qui l'a
    écrite (ajout, mise à jour, révocation). Un hôte dont le clone n'a pas
    encore vu ce commit (pas de `--fetch`) ne doit pas réappliquer un canon
    plus ancien : il réactiverait un authentificateur retiré depuis. Rend
    (lignes en avance sur le canon lu, sources du registre absentes du canon
    lu). Une ligne hors du format des fiches (écrite à la main) n'est pas un
    repère : la synchronisation la traite comme L6 le prévoit (révoquée si le
    canon ne la déclare pas).
    """
    sources = {s.member: s for s in canon.sources if s.mode == "git"}
    rows = storage.of(db).authenticators.canon_refs(receipts.SYNC_REVOCATIONS)
    ahead: list[str] = []
    unread: set[str] = set()
    seen: dict[tuple[str, str], bool] = {}
    for row in sorted(rows, key=lambda r: r["canon_ref"] or ""):
        match = receipts.FICHE_REF_RE.fullmatch(row["canon_ref"] or "")
        if not match:
            continue
        member, _path, commit = match.groups()
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
    reasons += ["source %s du registre non lue" % member for member in sorted(unread)]
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
                        bootstrap_ref: str = "", host: str = "") -> AuthenticatorSync:
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

    `trusted_ref` None (défaut) : la configuration de la connexion
    (`configured_ref(db)`) ; "" : aucune. Un refus rend AUTH_REFUSED et
    n'écrit rien ; une erreur de base annule la transaction entière.
    """
    if trusted_ref is None:
        trusted_ref = configured_ref(db)
    if not canon.readable:
        return AuthenticatorSync(AUTH_SKIPPED, "canon illisible : registre inchangé")
    if canon.untrusted:
        return AuthenticatorSync(AUTH_SKIPPED, "canon NON APPROUVÉ (fichiers de travail) : "
                                               "registre des authentificateurs inchangé")
    if not canon.sources:
        return AuthenticatorSync(AUTH_SKIPPED, "aucune source lue : registre inchangé")
    try:
        return storage.of(db).authenticators.under_registry_lock(
            lambda lock: _sync_locked(lock, canon, trusted_ref, bootstrap_ref, host))
    except receipts.RegistryLockError as exc:
        return AuthenticatorSync(AUTH_REFUSED, "%s : transaction annulée" % exc)
    except _Concurrent as exc:
        return AuthenticatorSync(AUTH_REFUSED, "%s : transaction annulée, registre des "
                                               "authentificateurs inchangé" % exc)
    except DbError as exc:
        return AuthenticatorSync(AUTH_REFUSED, "base : %s — transaction annulée, registre "
                                               "des authentificateurs inchangé" % exc)


def _sync_locked(lock: receipts.RegistryLock, canon: Canon, trusted_ref: str,
                 bootstrap_ref: str, host: str) -> AuthenticatorSync:
    """Contrôles et écritures, dans la transaction et sous le verrou du
    registre (`lock` : son jeton ; `lock.db` : la transaction)."""
    tx = lock.db
    last = last_applied(tx)
    try:
        branch, trust, notes = _verify(canon, last, trusted_ref, bootstrap_ref)
    except _Refused as exc:
        return AuthenticatorSync(AUTH_REFUSED, str(exc))
    ahead, unread = _lag(tx, canon)
    if ahead:
        return AuthenticatorSync(AUTH_REFUSED, (
            "canon en retard sur le registre des authentificateurs (déjà appliqué : %s) : "
            "registre inchangé — ameesh canon sync --fetch" % ", ".join(ahead[:5])),
            trust=trust, branch=branch, notes=notes)
    partial = _partial(canon, unread)
    # contrôles faits, verrou détenu : la seule écriture du registre
    result = receipts._apply_authenticators(
        lock, registry_members(canon), revoke_absent=not partial,
        source_commits={s.member: s.commit for s in canon.sources})
    _record(tx, last, canon, branch, trust, host, result)
    return AuthenticatorSync(AUTH_SYNCED, "", result, partial, trust=trust, branch=branch,
                             notes=notes)


def _member_of(canon_ref: str | None) -> str:
    return (canon_ref or "").split(":", 1)[0]


def _write_declared(db: Db, name: str, values: dict) -> None:
    """Écrit les colonnes déclaratives ; `placement_profile` est le profil des
    valeurs écrites (celles que `placement.evaluate` a jugées), calculé par
    `ameesh_placement_profile` dans la même instruction."""
    storage.of(db).canon.write_declared(name, values)


def _clear_responsible(db: Db, name: str, host: str) -> None:
    storage.of(db).canon.clear_responsible(name, host)


def _set_placement(db: Db, name: str, host: str, verdict: placement_mod.Verdict) -> None:
    """Écrit un verdict de placement et son profil évalué (colonnes
    déclaratives seulement), s'il change.

    `verdict.profile` : le profil évalué (texte de `ameesh_placement_profile`).
    None pour un refus jugé sur la ligne telle qu'elle est : son profil
    courant, calculé dans la même instruction. Un verdict admis n'est jamais
    écrit sans profil évalué (il vaudrait pour n'importe quel profil).
    """
    if verdict.ok and verdict.profile is None:
        raise ValueError("verdict de placement admis sans profil évalué : %s" % name)
    storage.of(db).canon.set_placement(name, host, ok=verdict.ok,
                                       diagnostic=verdict.diagnostic, ref=verdict.ref,
                                       profile=verdict.profile)


def _lineage_root(by_name: dict[str, dict], creator: str | None) -> tuple[dict | None, str]:
    """Créateur racine d'un éphémère, suivi comme `registry.canon_governed_sql`.

    (ligne, '') : premier ancêtre non éphémère ou venu du canon ;
    (None, raison) : lignée rompue ou trop longue (tenue pour gouvernée).
    """
    name = creator
    for _depth in range(registry._LINEAGE_MAX):
        row = by_name.get(name) if name else None
        if row is None:
            return None, "créateur %s introuvable" % (name or "non déclaré")
        if not row.get("ephemeral") or row.get("canon_ref"):
            return row, ""
        name = row.get("created_by")
    return None, "lignée de plus de %d créateurs" % registry._LINEAGE_MAX


def _inherit_placements(db: Db, host: str) -> None:
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
        root, broken = _lineage_root(by_name, row.get("created_by"))
        if root is not None and not root.get("canon_ref"):
            continue                    # lignée d'un agent inscrit à la main
        if root is None:
            verdict = placement_mod.Verdict(
                row["name"], host, False,
                "lignée invérifiable (%s) : placement non hérité" % broken)
        elif root.get("host") != host:
            verdict = placement_mod.Verdict(
                row["name"], host, False,
                "créateur %s placé sur %s, pas sur %s" % (
                    root["name"], root.get("host") or "?", host),
                root.get("placement_ref"))
        elif root.get("placement_ok") \
                and row.get("profile_now") != root.get("placement_profile"):
            # le verdict admis du créateur a jugé un autre profil que celui de
            # l'éphémère : rien n'en est hérité
            verdict = placement_mod.Verdict(
                row["name"], host, False,
                "profil divergé de celui évalué pour le créateur %s (évalué : %s ; "
                "éphémère : %s) : placement non hérité" % (
                    root["name"], root.get("placement_profile") or "aucun",
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
        if key == "capabilities":
            old = None if old is None else list(old)
        if old != new:
            return False
    return True


def sync(db: Db, canon: Canon, host: str, findings: list[Finding] | None = None, *,
         trusted_ref: str | None = None, bootstrap_ref: str = "",
         forge=None) -> SyncReport:
    """Aligne le registre de `host` sur le canon et enregistre l'état du canon.

    Canon illisible : seul l'état `unreadable` est écrit, puis CanonUnreadable.
    Ordre fail closed : un état non `ok` est écrit AVANT toute autre écriture
    (la réclamation est fermée d'abord) ; un état `ok` APRÈS (elle ne rouvre
    qu'une fois le registre aligné).

    `trusted_ref` (configuration de l'hôte) et `bootstrap_ref` (premier
    amorçage explicite) ne servent qu'au registre des authentificateurs :
    voir `sync_authenticators`. `trusted_ref` non fourni (None) : sync le
    prend lui-même dans la configuration de la connexion
    (`db.cfg.canon_ref`, AMEESH_CANON_REF) — l'exécuteur, qui ne le passe
    pas, suit donc la même priorité que `ameesh canon sync` ; "" : aucune.

    L'issue des authentificateurs est inscrite dans `canon_state`
    (`auth_status`, `auth_diagnostic`) et, en cas d'erreur, dans le fil
    (`record_authenticators`) : jamais silencieuse, quel que soit l'appelant.
    """
    if trusted_ref is None:
        trusted_ref = configured_ref(db)
    if not canon.readable:
        status, diagnostic = assess(canon, host)
        record_state(db, host, status, diagnostic, canon)
        raise CanonUnreadable(host, diagnostic, list(canon.load_findings))
    if findings is None:
        findings = canon_mod.validate(canon)
    status, diagnostic = assess(canon, host, findings)
    if status != CANON_OK:
        record_state(db, host, status, diagnostic, canon)
    block = canon_mod.blocking(findings)
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

    rows = _rows(db, host, sorted(set(admitted_here) | set(other_admitted)))

    for name in sorted(admitted_here):
        admission = admitted_here[name]
        agent = canon.agent(name)
        if agent is None or not NAME_RE.match(name):
            continue
        row = rows.get(name)
        # Hôte courant admis et différent de celui qu'on synchronise : on ne
        # touche à rien, l'exécution reste où elle est (0029).
        if row is not None and row.get("host") not in (None, host) \
                and row.get("host") in other_admitted.get(name, []):
            actions.append(SyncAction(
                name, "laissé",
                "tourne sur %s, hôte admis : « canon sync » ne le déplace pas"
                % row.get("host")))
            continue
        reasons = block.reasons(name, [host])
        responsible = canon.resolve_human(agent.responsible) if not reasons else None
        verdict = placement_mod.evaluate(canon, name, host)
        # L31 (0029) : règle de visibilité, repliée dans le verdict de placement
        # (fail closed ; sans dépôt de mémoire, sans objet).
        visibility_ok, visibility_diag = visibility_mod.annotate(
            canon, agent, host, verdict, db=db, forge=forge)
        # Dossier de travail (L31, 0029 ; L35) : politique de l'hôte d'abord
        # (`work_dirs[agent]`, `work_roots[projet]`, `work_root`), repli
        # transitoire sur le `cwd` d'une ancienne fiche Placement, noté ici.
        cwd, cwd_source = canon_mod.work_dir_for(canon.host(host), agent, admission)
        notes: list[str] = []
        if cwd_source == canon_mod.WORK_FROM_PLACEMENT:
            notes.append("cwd hérité de la fiche Placement (%s), à migrer vers "
                         "policy.work_dirs / work_roots de l'hôte" % cwd)
        elif cwd is None:
            notes.append("aucun dossier de travail : déclarer policy.work_dirs / "
                         "work_roots / work_root dans la fiche de l'hôte")
        values = {
            "harness": agent.harness or "other",
            "host": host,
            "cwd": os.path.expanduser(cwd) if cwd else None,
            "model": agent.model,
            "budget_usd": agent.budget_usd_per_day,
            "responsible": responsible,
            "team": agent.team,
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
        }
        if row is None:
            _write_declared(db, name, values)
            action = SyncAction(name, "créé")
        elif _same(row, values):
            action = SyncAction(name, "inchangé")
        else:
            _write_declared(db, name, values)
            action = SyncAction(name, "mis à jour")
        action.placement = verdict
        if reasons:
            action.blocked = reasons
            notes.insert(0, "non réclamable : erreur bloquante du canon")
        if not verdict.ok:
            notes.insert(0, "non réclamable : placement refusé — %s" % verdict.diagnostic)
        action.detail = " ; ".join(notes)
        actions.append(action)
        if row is not None and row.get("status") == "stopped" \
                and (row.get("status_text") or "").startswith(STOP_MARK):
            if storage.of(db).canon.revive(name, REVIVED_TEXT, STOP_MARK):
                actions.append(SyncAction(name, "réintégré", "arrêté par sync, de retour au canon"))
        elif row is not None and (row.get("status_text") or "").startswith(PENDING_MARK):
            storage.of(db).canon.clear_pending_stop(name, PENDING_MARK)

    loaded = canon.loaded_members()
    for name, row in sorted(rows.items()):
        if name in admitted_here or row.get("host") != host:
            continue
        if row.get("ephemeral") or not row.get("canon_ref"):
            continue  # éphémère, ou jamais venu du canon : sync ne le gouverne pas
        member = _member_of(row.get("canon_ref"))
        unverifiable = ("membre %s non lu" % member if member not in loaded
                        else "canon invalide" if block.global_errors else "")
        if unverifiable:
            # Fiche invérifiable : ni arrêté (le retrait n'est pas constaté), ni
            # réclamable (son responsable n'est plus confirmé par le canon, son
            # placement non plus).
            _clear_responsible(db, name, host)
            verdict = placement_mod.Verdict(
                name, host, False, "placement invérifiable : %s" % unverifiable,
                row.get("placement_ref"))
            _set_placement(db, name, host, verdict)
            actions.append(SyncAction(
                name, "laissé", "%s : retrait différé, non réclamable" % unverifiable,
                block.reasons(name) if block.global_errors else [], verdict))
            continue
        cibles = other_admitted.get(name)
        if cibles and canon.agent(name) is not None:
            target = cibles[0]
            # L'hôte courant n'est plus admis, mais un autre hôte l'est : le
            # canon le déplace là (jamais hors d'un hôte admis).
            verdict = placement_mod.Verdict(
                name, target, None,
                "déplacé de %s : hôte admis %s, à évaluer par « ameesh canon sync » sur %s"
                % (host, target, target), row.get("placement_ref"))
            storage.of(db).canon.move_host(
                name, host, target=target, canon_ref=canon.agent(name).fiche.ref,
                diagnostic=verdict.diagnostic, ref=verdict.ref)
            actions.append(SyncAction(
                name, "déplacé",
                "admis sur %s : non réclamable avant la synchronisation de cet hôte" % target,
                placement=verdict))
            continue
        why = "plus d'admission" if canon.agent(name) is not None else "fiche absente"
        # Sans admission sur cet hôte, plus réclamable ici, quel que soit son
        # statut (fin de tour comprise) : écrit avant l'arrêt, colonnes
        # déclaratives seulement.
        verdict = placement_mod.evaluate(canon, name, host)
        _set_placement(db, name, host, verdict)
        stopped = storage.of(db).canon.stop_removed(
            name, host, pending_text="%s (%s)" % (PENDING_MARK, why),
            stop_text="%s (%s)" % (STOP_MARK, why))
        if stopped is None:
            continue
        if stopped["status"] == "stopped":
            actions.append(SyncAction(name, "arrêté", why, placement=verdict))
        else:
            actions.append(SyncAction(name, "arrêt demandé",
                                      "%s : tour en cours, rien n'est tué" % why,
                                      placement=verdict))
    for name, row in sorted(rows.items()):
        if row.get("host") == host and not row.get("canon_ref") and not row.get("ephemeral") \
                and name not in admitted_here:
            actions.append(SyncAction(name, "hors canon",
                                      "inscrit à la main : non gouverné par sync"))
    _inherit_placements(db, host)
    packages = sync_packages(db, canon, findings)
    if status == CANON_OK:
        record_state(db, host, status, diagnostic, canon)
    # registre des authentificateurs (§8.2) : copie de travail des fiches
    # Member, quel que soit l'état du canon pour cet hôte (ses propres règles)
    authenticators = sync_authenticators(db, canon, trusted_ref=trusted_ref,
                                         bootstrap_ref=bootstrap_ref, host=host)
    # jamais silencieux : diagnostic durable (canon_state) et fil
    record_authenticators(db, host, authenticators)
    return SyncReport(host=host, source=canon.source_label(), untrusted=canon.untrusted,
                      actions=actions, findings=findings, status=status,
                      diagnostic=diagnostic, authenticators=authenticators,
                      packages=packages)


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

    def to_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated,
                "unchanged": self.unchanged, "retired": self.retired,
                "skipped": self.skipped, "relinked_work_items": self.relinked}


_PACKAGE_KEYS = ("kind", "title", "parent", "responsible", "team", "scope", "status",
                 "canon_ref")


def sync_packages(db: Db, canon: Canon, findings: list[Finding]) -> PackageSync:
    """Recopie les fiches WorkPackage dans `work_packages` (une transaction)."""
    done = PackageSync()
    bad = {f.package for f in canon_mod.errors(findings) if f.package}
    loaded = canon.loaded_members()
    with db.transaction() as tx:
        st = storage.of(tx)
        before = {row["id"]: row for row in st.packages.all(include_absent=True)}
        seen: set[str] = set()
        for package in canon.packages:
            seen.add(package.id)
            if package.id in bad:
                done.skipped.append(package.id)
                continue
            row = {"id": package.id, "kind": package.kind, "title": package.title,
                   "parent": package.parent, "responsible": package.responsible,
                   "team": package.team, "scope": package.scope, "status": package.status,
                   "canon_ref": package.fiche.ref}
            old = before.get(package.id)
            if old is not None and old.get("present") and all(
                    (old.get(k) or None) == (row.get(k) or None) for k in _PACKAGE_KEYS):
                done.unchanged.append(package.id)
                continue
            st.packages.upsert(row)
            (done.created if old is None else done.updated).append(package.id)
        gone = [ident for ident, row in before.items()
                if row.get("present") and ident not in seen
                and _member_of(row.get("canon_ref")) in loaded]
        st.packages.retire(gone)
        done.retired = sorted(gone)
        done.relinked = st.work.refresh_package_parents()
    return done


# --------------------------------------------------------------------------
# agents éphémères (R14)
# --------------------------------------------------------------------------

def parse_ttl(text: str | None) -> float:
    """« 30m », « 2h », « 1d », « 3600 » → secondes, borné à 7 jours. Obligatoire."""
    if not text:
        raise SpawnError("échéance obligatoire : --ttl 30m, 2h, 1d…")
    match = _TTL_RE.match(text)
    if not match:
        raise SpawnError("échéance illisible : %r (attendu 30m, 2h, 1d ou des secondes)" % text)
    seconds = float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[
        match.group(2).lower()]
    if seconds <= 0:
        raise SpawnError("échéance nulle")
    if seconds > MAX_EPHEMERAL_TTL:
        raise SpawnError("échéance trop lointaine pour un éphémère (maximum %d jours)"
                         % (MAX_EPHEMERAL_TTL // 86400))
    return seconds


def spawn(db: Db, name: str, creator: str, ttl_seconds: float, *,
          cwd: str | None = None, canon: Canon | None = None) -> dict:
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
        raise SpawnError("nom d'agent invalide : %r" % (name,))
    if not NAME_RE.match(creator or ""):
        raise SpawnError("créateur invalide : %r" % (creator,))
    if name == creator:
        raise SpawnError("un agent ne se crée pas lui-même")
    if canon is not None and canon.readable and canon.agent(name) is not None:
        raise SpawnError("%s a une fiche dans le canon : ce n'est pas un éphémère" % name)
    parent = storage.of(db).ephemerals.creator(creator)
    if parent is None:
        raise SpawnError("créateur inconnu du registre : %s" % creator)
    if not (parent.get("responsible") or "").strip():
        raise SpawnError("le créateur %s n'a pas d'humain responsable résolu (R14) : "
                         "il ne peut pas transmettre de responsabilité" % creator)
    if not parent.get("alive"):
        raise SpawnError("le créateur %s est un éphémère échu" % creator)
    if parent.get("status") == "stopped":
        raise SpawnError("le créateur %s est arrêté" % creator)
    created = storage.of(db).ephemerals.create(
        name, creator, cwd=os.path.abspath(os.path.expanduser(cwd)) if cwd else None,
        ttl_seconds=ttl_seconds)
    if created is None:
        if storage.of(db).ephemerals.exists(name):
            raise SpawnError("un agent %s existe déjà" % name)
        raise SpawnError("le créateur %s a changé pendant la création : réessayez" % creator)
    return created
