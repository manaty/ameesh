# SPDX-License-Identifier: AGPL-3.0-only
"""Du canon vers le registre (spec §4.4) : `canon sync` et agents éphémères.

`sync` recopie aussi les authentificateurs des fiches Member dans le
registre de confiance (`authenticators`, §8.2), par
`receipts.sync_authenticators` (L6), depuis le canon lu à sa révision
canonique seulement (voir `sync_authenticators`).

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
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from . import canon as canon_mod
from . import placement as placement_mod
from . import receipts, registry
from .canon import Canon, Finding
from .config import NAME_RE
from .db import Db

#: préfixes de `status_text` posés par sync (et reconnus par lui seul)
STOP_MARK = "retiré du canon"
PENDING_MARK = "arrêt demandé en fin de tour : retiré du canon"
REVIVED_TEXT = "réintégré au canon"

MAX_EPHEMERAL_TTL = 7 * 24 * 3600.0
_TTL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.I)

DECLARATIVE = ("harness", "host", "cwd", "model", "budget_usd", "responsible", "team",
               "provider", "credential_mode", "capabilities", "canon_ref", "ephemeral",
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
AUTH_SYNCED = "synced"       # receipts.sync_authenticators appelé
AUTH_SKIPPED = "skipped"     # rien écrit, sans erreur (canon non approuvé : tests)
AUTH_REFUSED = "refused"     # rien écrit, ERREUR (révision hors branche, canon en retard)


@dataclass
class AuthenticatorSync:
    """Canon → registre des authentificateurs (§8.2) : ce qui a été fait."""

    status: str
    reason: str = ""
    #: résultat de `receipts.sync_authenticators` (statut `synced`)
    result: dict | None = None
    #: canon lu en partie : aucune révocation pour absence (raisons)
    partial: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        if self.status == AUTH_REFUSED:
            return [self.reason]
        return list((self.result or {}).get("errors") or [])

    def to_dict(self) -> dict:
        out: dict = {"status": self.status, "reason": self.reason,
                     "errors": self.errors, "partial": self.partial}
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

    @property
    def errors(self) -> list[Finding]:
        return canon_mod.errors(self.findings)

    def to_dict(self) -> dict:
        return {"host": self.host, "source": self.source, "untrusted": self.untrusted,
                "canon_status": self.status, "diagnostic": self.diagnostic,
                "actions": [a.to_dict() for a in self.actions],
                "errors": len(self.errors),
                "findings": [f.to_dict() for f in self.findings],
                "authenticators": (self.authenticators.to_dict()
                                   if self.authenticators is not None else None)}


# --------------------------------------------------------------------------
# état du canon par hôte (canon_state, §4.1)
# --------------------------------------------------------------------------

def _describe(findings: list[Finding]) -> str:
    text = " ; ".join("%s (%s) : %s" % (f.code, f.where(), f.message) for f in findings)
    return text if len(text) <= _DIAGNOSTIC_MAX else text[:_DIAGNOSTIC_MAX - 1] + "…"


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
        return CANON_OK, "erreurs propres à des agents (non réclamables) : " + _describe(others)
    return CANON_OK, ""


def record_state(db: Db, host: str, status: str, diagnostic: str,
                 canon: Canon | None = None) -> None:
    """Écrit l'état du canon de `host` ; le dernier commit valide ne suit que `ok`."""
    commit = None
    if status == CANON_OK and canon is not None and canon.sources:
        commit = canon.sources[0].commit or None
    db.query(
        """
        INSERT INTO canon_state AS s
            (host, status, root, source, last_good_commit, last_good_at, diagnostic,
             checked_at)
        VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, %s, now())
        ON CONFLICT (host) DO UPDATE SET
            status           = excluded.status,
            root             = excluded.root,
            source           = excluded.source,
            last_good_commit = CASE WHEN excluded.status = 'ok'
                                    THEN excluded.last_good_commit
                                    ELSE s.last_good_commit END,
            last_good_at     = CASE WHEN excluded.status = 'ok'
                                    THEN excluded.last_good_at
                                    ELSE s.last_good_at END,
            diagnostic       = excluded.diagnostic,
            checked_at       = excluded.checked_at
        RETURNING host
        """,
        (host, status, canon.root if canon is not None else "",
         canon.source_label() if canon is not None and canon.sources else "",
         commit, status == CANON_OK, diagnostic),
    )


def state(db: Db, host: str) -> dict | None:
    """Dernier état enregistré du canon de `host`, ou None."""
    rows = db.query(
        """
        SELECT host, status, root, source, last_good_commit, diagnostic,
               extract(epoch from last_good_at)::float8 AS last_good_ts,
               extract(epoch from checked_at)::float8   AS checked_ts
          FROM canon_state WHERE host = %s
        """,
        (host,),
    )
    return rows[0] if rows else None


# --------------------------------------------------------------------------
# registre des authentificateurs (§8.2, L6)
# --------------------------------------------------------------------------

def _canonical_rev(canon: Canon, source: canon_mod.Source) -> str:
    """La branche canonique distante d'une source : `ref` de son entrée au
    manifeste (`origin/<ref>`), sinon la branche par défaut de l'amorçage."""
    manifest = canon.federation if isinstance(canon.federation, dict) else {}
    root = canon.sources[0]
    member = source.member
    if source.repo == root.repo and source.commit == root.commit:
        member = root.member            # même instantané que la racine
    entry = next((m for m in manifest.get("members") or []
                  if isinstance(m, dict) and str(m.get("id")) == member), None)
    branch = str((entry or {}).get("ref") or "")
    if branch:
        return "%s/%s" % (canon_mod.DEFAULT_REMOTE, branch)
    if member == root.member:
        for rev in ("%s/%s" % (canon_mod.DEFAULT_REMOTE, canon_mod.DEFAULT_BRANCH),
                    "%s/HEAD" % canon_mod.DEFAULT_REMOTE):
            if canon_mod.git_commit(source.repo, rev):
                return rev
    return "%s/%s" % (canon_mod.DEFAULT_REMOTE, canon_mod.DEFAULT_BRANCH)


def _off_branch(canon: Canon) -> list[str]:
    """Sources lues à un commit absent de leur branche canonique distante
    (§4.1 : un tel commit est ignoré ; ex. `--ref` sur un commit local, ou
    sur une branche de PR poussée mais pas fusionnée). La branche canonique
    est celle du manifeste (sinon origin/main) : `--ref` / AMEESH_CANON_REF
    choisit ce qu'on lit, jamais ce qui fait foi pour le registre de
    confiance."""
    problems = []
    for source in canon.sources:
        if source.mode != "git":
            problems.append("%s : source non approuvée" % source.member)
            continue
        rev = _canonical_rev(canon, source)
        head = canon_mod.git_commit(source.repo, rev)
        if not head or not canon_mod.git_contains(source.repo, source.commit, head):
            problems.append("%s @ %s : commit absent de la branche canonique %s"
                            % (source.member, source.commit[:12], rev))
    return problems


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
    rows = db.query(
        "SELECT DISTINCT canon_ref FROM authenticators "
        "WHERE revoked_at IS NULL OR revoked_reason IN (%s, %s)",
        receipts.SYNC_REVOCATIONS)
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
    """Les fiches Member au format de `receipts.sync_authenticators`.

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


def sync_authenticators(db: Db, canon: Canon) -> AuthenticatorSync:
    """Canon → registre des authentificateurs (§8.2), par `receipts.sync_authenticators`.

    Écriture de confiance : rien n'est écrit si le canon est illisible, lu
    hors git (non approuvé), lu à un commit absent de sa branche canonique
    distante, ou en retard sur le registre (une ligne vient d'un commit que
    le canon lu ne contient pas : `ameesh canon sync --fetch`). Sinon, les
    règles de L6 s'appliquent telles quelles (entrée invalide : membre gelé,
    aucune écriture pour lui ; absent du canon : révoqué) ; un canon lu en
    partie (erreur de lecture, membre de la fédération absent) ne révoque
    rien pour absence.
    """
    if not canon.readable:
        return AuthenticatorSync(AUTH_SKIPPED, "canon illisible : registre inchangé")
    if canon.untrusted:
        return AuthenticatorSync(AUTH_SKIPPED, "canon NON APPROUVÉ (fichiers de travail) : "
                                               "registre des authentificateurs inchangé")
    if not canon.sources:
        return AuthenticatorSync(AUTH_SKIPPED, "aucune source lue : registre inchangé")
    off = _off_branch(canon)
    if off:
        return AuthenticatorSync(AUTH_REFUSED, "révision non canonique, registre des "
                                               "authentificateurs inchangé : " + " ; ".join(off))
    ahead, unread = _lag(db, canon)
    if ahead:
        return AuthenticatorSync(AUTH_REFUSED, (
            "canon en retard sur le registre des authentificateurs (déjà appliqué : %s) : "
            "registre inchangé — ameesh canon sync --fetch" % ", ".join(ahead[:5])))
    partial = _partial(canon, unread)
    result = receipts.sync_authenticators(
        db, registry_members(canon), revoke_absent=not partial,
        source_commits={s.member: s.commit for s in canon.sources})
    return AuthenticatorSync(AUTH_SYNCED, "", result, partial)


def _member_of(canon_ref: str | None) -> str:
    return (canon_ref or "").split(":", 1)[0]


def _caps_param(capabilities) -> str | None:
    return None if capabilities is None else json.dumps(list(capabilities))


_CAPS_SQL = ("CASE WHEN %s::text IS NULL THEN NULL "
             "ELSE ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)) END")


def _write_declared(db: Db, name: str, values: dict) -> None:
    """Écrit les colonnes déclaratives ; `placement_profile` est le profil des
    valeurs écrites (celles que `placement.evaluate` a jugées), calculé par
    `ameesh_placement_profile` dans la même instruction."""
    caps = _caps_param(values["capabilities"])
    db.query(
        """
        INSERT INTO agent_registry
            (name, harness, host, cwd, model, budget_usd, responsible, team, provider,
             credential_mode, capabilities, canon_ref, ephemeral, ephemeral_expires_at,
             placement_ok, placement_diagnostic, placement_ref, placement_profile)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, __CAPS__, %s, false, NULL,
                %s, %s, %s, __PROFILE__)
        ON CONFLICT (name) DO UPDATE SET
            harness         = excluded.harness,
            host            = excluded.host,
            cwd             = excluded.cwd,
            model           = excluded.model,
            budget_usd      = excluded.budget_usd,
            responsible     = excluded.responsible,
            team            = excluded.team,
            provider        = excluded.provider,
            credential_mode = excluded.credential_mode,
            capabilities    = excluded.capabilities,
            canon_ref       = excluded.canon_ref,
            ephemeral       = false,
            ephemeral_expires_at = NULL,
            placement_ok    = excluded.placement_ok,
            placement_diagnostic = excluded.placement_diagnostic,
            placement_ref   = excluded.placement_ref,
            placement_profile = excluded.placement_profile,
            updated_at      = now()
        RETURNING name
        """.replace("__CAPS__", _CAPS_SQL)
           .replace("__PROFILE__", placement_mod.PROFILE_PARAMS_SQL),
        (name, values["harness"], values["host"], values["cwd"], values["model"],
         values["budget_usd"], values["responsible"], values["team"], values["provider"],
         values["credential_mode"], caps, caps, values["canon_ref"],
         values["placement_ok"], values["placement_diagnostic"], values["placement_ref"])
        + placement_mod.profile_params(values),
    )


def _clear_responsible(db: Db, name: str, host: str) -> None:
    db.execute(
        "UPDATE agent_registry SET responsible = NULL, updated_at = now() "
        "WHERE name = %s AND host = %s AND NOT ephemeral AND responsible IS NOT NULL",
        (name, host))


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
    db.execute(
        """
        UPDATE agent_registry
           SET placement_ok = %s, placement_diagnostic = %s, placement_ref = %s,
               placement_profile = coalesce(%s::text, __PROFILE__),
               updated_at = now()
         WHERE name = %s AND host = %s
           AND (placement_ok, placement_diagnostic, placement_ref, placement_profile)
               IS DISTINCT FROM (%s::boolean, %s::text, %s::text,
                                 coalesce(%s::text, __PROFILE__))
        """.replace("__PROFILE__", placement_mod.profile_sql()),
        (verdict.ok, verdict.diagnostic, verdict.ref, verdict.profile, name, host,
         verdict.ok, verdict.diagnostic, verdict.ref, verdict.profile))


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
    rows = db.query(
        """
        SELECT name, created_by, ephemeral, canon_ref, host,
               placement_ok, placement_diagnostic, placement_ref, placement_profile,
               __PROFILE__ AS profile_now
          FROM agent_registry
         WHERE ephemeral
            OR name IN (SELECT created_by FROM agent_registry
                         WHERE ephemeral AND created_by IS NOT NULL)
        """.replace("__PROFILE__", placement_mod.profile_sql())
    )
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
    sql = """
        SELECT name, harness, host, cwd, model, budget_usd, responsible, team, provider,
               credential_mode, capabilities, canon_ref, ephemeral, status, status_text,
               placement_ok, placement_diagnostic, placement_ref,
               placement_profile IS NOT DISTINCT FROM __PROFILE__ AS profile_fresh,
               (status = 'running' AND lease_expires_at > now()) AS in_turn
          FROM agent_registry
         WHERE host = %s
    """.replace("__PROFILE__", placement_mod.profile_sql())
    params: list = [host]
    if names:
        sql += " OR name = ANY(ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)))"
        params.append(json.dumps(names))
    return {row["name"]: row for row in db.query(sql, tuple(params))}


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


def sync(db: Db, canon: Canon, host: str, findings: list[Finding] | None = None) -> SyncReport:
    """Aligne le registre de `host` sur le canon et enregistre l'état du canon.

    Canon illisible : seul l'état `unreadable` est écrit, puis CanonUnreadable.
    Ordre fail closed : un état non `ok` est écrit AVANT toute autre écriture
    (la réclamation est fermée d'abord) ; un état `ok` APRÈS (elle ne rouvre
    qu'une fois le registre aligné).
    """
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

    placed_here: dict[str, canon_mod.Placement] = {}
    elsewhere: dict[str, list[canon_mod.Placement]] = {}
    for agent in canon.agents:
        placements = [p for p in canon.placements_of(agent.title)]
        here = [p for p in placements if p.host == host]
        if here:
            placed_here.setdefault(agent.title, here[0])
        elif placements:
            elsewhere[agent.title] = placements

    rows = _rows(db, host, sorted(placed_here))

    for name in sorted(placed_here):
        placement = placed_here[name]
        agent = canon.agent(name)
        if agent is None or not NAME_RE.match(name):
            continue
        reasons = block.reasons(name, [host])
        responsible = canon.resolve_human(agent.responsible) if not reasons else None
        verdict = placement_mod.evaluate(canon, name, host)
        values = {
            "harness": agent.harness or "other",
            "host": host,
            "cwd": os.path.expanduser(placement.cwd) if placement.cwd else None,
            "model": agent.model,
            "budget_usd": agent.budget_usd_per_day,
            "responsible": responsible,
            "team": agent.team,
            "provider": agent.provider,
            "credential_mode": placement.credential_mode or agent.credential_mode,
            # `approve` n'entre jamais au registre (R8) ; la fiche est de toute
            # façon bloquée par l'erreur agent-approve-capability
            "capabilities": None if agent.capabilities is None else [
                c for c in agent.capabilities
                if c.strip().lower() not in canon_mod.FORBIDDEN_CAPABILITIES],
            "canon_ref": agent.fiche.ref,
            "ephemeral": False,
            # C4 : admissibilité du placement sur CET hôte (politique de la fiche Host),
            # pour le profil de ces valeurs (`_write_declared` écrit le profil évalué ;
            # un harnais non déclaré, refusé par toute politique qui restreint les
            # harnais, est inscrit « other »)
            "placement_ok": verdict.ok,
            "placement_diagnostic": verdict.diagnostic,
            "placement_ref": verdict.ref,
        }
        row = rows.get(name)
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
            action.detail = "non réclamable : erreur bloquante du canon"
        if not verdict.ok:
            action.detail = "non réclamable : placement refusé — %s" % verdict.diagnostic
        actions.append(action)
        if row is not None and row.get("status") == "stopped" \
                and (row.get("status_text") or "").startswith(STOP_MARK):
            revived = db.query(
                """
                UPDATE agent_registry
                   SET status = 'idle', status_text = %s, updated_at = now()
                 WHERE name = %s AND status = 'stopped' AND starts_with(status_text, %s)
                RETURNING name
                """,
                (REVIVED_TEXT, name, STOP_MARK),
            )
            if revived:
                actions.append(SyncAction(name, "réintégré", "arrêté par sync, de retour au canon"))
        elif row is not None and (row.get("status_text") or "").startswith(PENDING_MARK):
            db.execute(
                "UPDATE agent_registry SET status_text = '' "
                "WHERE name = %s AND starts_with(status_text, %s)",
                (name, PENDING_MARK))

    loaded = canon.loaded_members()
    for name, row in sorted(rows.items()):
        if name in placed_here or row.get("host") != host:
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
        moved = elsewhere.get(name)
        if moved and len(moved) == 1 and canon.agent(name) is not None:
            target = moved[0].host or ""
            # Le canon l'a placé ailleurs (décision humaine) : sync suit, et
            # le placement n'est évalué que par la synchronisation de l'hôte cible.
            verdict = placement_mod.Verdict(
                name, target, None,
                "déplacé de %s : placement à évaluer par « ameesh canon sync » sur %s"
                % (host, target), moved[0].fiche.ref)
            db.execute(
                """
                UPDATE agent_registry
                   SET host = %s, responsible = NULL, canon_ref = %s,
                       placement_ok = NULL, placement_diagnostic = %s, placement_ref = %s,
                       placement_profile = NULL,
                       updated_at = now()
                 WHERE name = %s AND host = %s
                """,
                (target, canon.agent(name).fiche.ref, verdict.diagnostic, verdict.ref,
                 name, host))
            actions.append(SyncAction(
                name, "déplacé",
                "placé sur %s : non réclamable avant la synchronisation de cet hôte" % target,
                placement=verdict))
            continue
        why = "plus de placement" if canon.agent(name) is not None else "fiche absente"
        # Sans placement sur cet hôte, plus réclamable ici, quel que soit son
        # statut (fin de tour comprise) : écrit avant l'arrêt, colonnes
        # déclaratives seulement.
        verdict = placement_mod.evaluate(canon, name, host)
        _set_placement(db, name, host, verdict)
        stopped = db.query(
            """
            UPDATE agent_registry
               SET status = CASE WHEN status = 'running' AND lease_expires_at > now()
                                 THEN status ELSE 'stopped' END,
                   status_text = CASE WHEN status = 'running' AND lease_expires_at > now()
                                      THEN %s ELSE %s END,
                   updated_at = now()
             WHERE name = %s AND host = %s AND status <> 'stopped'
               AND NOT ephemeral AND canon_ref IS NOT NULL
            RETURNING name, status
            """,
            ("%s (%s)" % (PENDING_MARK, why), "%s (%s)" % (STOP_MARK, why), name, host),
        )
        if not stopped:
            continue
        if stopped[0]["status"] == "stopped":
            actions.append(SyncAction(name, "arrêté", why, placement=verdict))
        else:
            actions.append(SyncAction(name, "arrêt demandé",
                                      "%s : tour en cours, rien n'est tué" % why,
                                      placement=verdict))
    for name, row in sorted(rows.items()):
        if row.get("host") == host and not row.get("canon_ref") and not row.get("ephemeral") \
                and name not in placed_here:
            actions.append(SyncAction(name, "hors canon",
                                      "inscrit à la main : non gouverné par sync"))
    _inherit_placements(db, host)
    if status == CANON_OK:
        record_state(db, host, status, diagnostic, canon)
    # registre des authentificateurs (§8.2) : copie de travail des fiches
    # Member, quel que soit l'état du canon pour cet hôte (ses propres règles)
    authenticators = sync_authenticators(db, canon)
    return SyncReport(host=host, source=canon.source_label(), untrusted=canon.untrusted,
                      actions=actions, findings=findings, status=status,
                      diagnostic=diagnostic, authenticators=authenticators)


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
    rows = db.query(
        """
        SELECT name, responsible, ephemeral, status, capabilities,
               (NOT ephemeral OR ephemeral_expires_at > now()) AS alive
          FROM agent_registry WHERE name = %s
        """,
        (creator,),
    )
    if not rows:
        raise SpawnError("créateur inconnu du registre : %s" % creator)
    parent = rows[0]
    if not (parent.get("responsible") or "").strip():
        raise SpawnError("le créateur %s n'a pas d'humain responsable résolu (R14) : "
                         "il ne peut pas transmettre de responsabilité" % creator)
    if not parent.get("alive"):
        raise SpawnError("le créateur %s est un éphémère échu" % creator)
    if parent.get("status") == "stopped":
        raise SpawnError("le créateur %s est arrêté" % creator)
    created = db.query(
        """
        INSERT INTO agent_registry
            (name, chantier, harness, host, cwd, model, responsible, team, provider,
             credential_mode, capabilities, ephemeral, ephemeral_expires_at, created_by,
             placement_ok, placement_diagnostic, placement_ref, placement_profile)
        SELECT %s, c.chantier, c.harness, c.host, coalesce(%s, c.cwd), c.model,
               c.responsible, c.team, c.provider, c.credential_mode,
               ARRAY(SELECT x FROM unnest(ARRAY['read', 'propose']::text[]) AS x
                      WHERE c.capabilities IS NULL OR x = ANY(c.capabilities)),
               true,
               CASE WHEN c.ephemeral
                    THEN least(now() + make_interval(secs => %s), c.ephemeral_expires_at)
                    ELSE now() + make_interval(secs => %s) END,
               c.name,
               -- C4 : le profil de l'éphémère est celui du créateur (colonnes
               -- recopiées ci-dessus) ; un verdict admis n'est hérité que s'il
               -- a jugé ce profil
               CASE WHEN c.placement_ok AND c.placement_profile IS DISTINCT FROM __NOW__
                    THEN false ELSE c.placement_ok END,
               CASE WHEN c.placement_ok AND c.placement_profile IS DISTINCT FROM __NOW__
                    THEN 'profil de ' || c.name || ' divergé depuis l''évaluation de son '
                         || 'placement (évalué : ' || coalesce(c.placement_profile, 'aucun')
                         || ' ; actuel : ' || __NOW__ || ') : placement non hérité, à '
                         || 'réévaluer par « ameesh canon sync »'
                    ELSE c.placement_diagnostic END,
               c.placement_ref, c.placement_profile
          FROM agent_registry c
         WHERE c.name = %s
           AND coalesce(c.responsible, '') <> ''
           AND c.status <> 'stopped'
           AND (NOT c.ephemeral OR c.ephemeral_expires_at > now())
        ON CONFLICT (name) DO NOTHING
        RETURNING name, responsible, host, harness, capabilities, created_by,
                  placement_ok, placement_diagnostic,
                  extract(epoch from ephemeral_expires_at)::float8 AS ephemeral_expires_ts
        """.replace("__NOW__", placement_mod.profile_sql("c")),
        (name, os.path.abspath(os.path.expanduser(cwd)) if cwd else None,
         ttl_seconds, ttl_seconds, creator),
    )
    if not created:
        if db.query("SELECT 1 AS x FROM agent_registry WHERE name = %s", (name,)):
            raise SpawnError("un agent %s existe déjà" % name)
        raise SpawnError("le créateur %s a changé pendant la création : réessayez" % creator)
    return created[0]
