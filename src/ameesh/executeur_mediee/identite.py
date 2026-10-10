# SPDX-License-Identifier: AGPL-3.0-only
"""Côté serveur (lot L110) : enrôlement, jetons, révocation des exécuteurs.

* `create_invitation` : le code d'enrôlement à usage unique, émis par un
  humain habilité (`ameesh host enroll`) ;
* `DbIdentityProvider` : l'implémentation de `interfaces.IdentityProvider`
  (`enroll`, `issue_access_token`, `issue_session_token`, `revoke`,
  `verify`) sur les tables de la migration 0110 ;
* `verify_token(db, token, *, kind=None) -> Principal` : la seule vue de
  l'identité dont L108 (serveur d'exécuteur) et L111 (relais de modèle) ont
  besoin. Lève `interfaces.AuthError` (`token_expired`, `token_invalid`,
  `executor_revoked`). L'état de l'exécuteur est relu au plus toutes les 5 s
  (`EXECUTOR_STATE_CACHE_S`) ;
* `revoke_host` : révoque tous les exécuteurs d'un hôte et annule ses
  invitations en attente.

Ce module ne lit ni n'écrit aucune table d'autorité (approbations, nonces,
autorisations permanentes, authentificateurs ; décision 0012) : un jeton
d'exécuteur ne porte aucun droit d'approbation, et aucun `Principal` n'en
décrit.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
import weakref
from typing import Any, Callable, Mapping, Optional

from ..config import NAME_RE
from . import contrat as C
from . import jose
from .jose import normalize_code
from .interfaces import (ACCESS_TOKEN_PREFIX, ACCESS_TOKEN_TTL_S, ASSERTION_MAX_TTL_S,
                         EXECUTOR_STATE_CACHE_S, SESSION_TOKEN_PREFIX, AuthError,
                         IdentityProvider, IssuedToken, Principal, ScopeError)

#: alphabet de Crockford (sans I, L, O, U)
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: 24 caractères de Crockford = 120 bits, affichés en 6 groupes de 4
CODE_CHARS = 24
CODE_GROUP = 4
INVITATION_TTL_DEFAULT_S = 15 * 60
INVITATION_TTL_MAX_S = 60 * 60
#: plafond dur d'un jeton de session : il meurt d'abord avec son bail
SESSION_TOKEN_MAX_S = 12 * 3600
#: tolérance d'horloge sur `iat` et `exp` des assertions
CLOCK_SKEW_S = 30
_TOKEN_RE = re.compile(r"^(amx1|ams1)\.[A-Za-z0-9_-]{43}$")
_JTI_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_EXECUTOR_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_CODE_RE = re.compile(r"^[%s]{%d}$" % (CROCKFORD, CODE_CHARS))


# --------------------------------------------------------------------------
# utilitaires
# --------------------------------------------------------------------------

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_code() -> str:
    raw = "".join(secrets.choice(CROCKFORD) for _ in range(CODE_CHARS))
    return "-".join(raw[i:i + CODE_GROUP] for i in range(0, CODE_CHARS, CODE_GROUP))


def new_token(prefix: str) -> str:
    return prefix + jose.b64u(secrets.token_bytes(32))


def _allowlist(value: Any) -> Optional[tuple]:
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    return tuple(value)


def _jsonb(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _event(db, kind: str, host: str, actor: str, executor_id: Optional[str] = None,
           **detail) -> None:
    db.query("INSERT INTO executor_events (kind, host, executor_id, actor, detail) "
             "VALUES (%s, %s, %s, %s, %s::jsonb) RETURNING id",
             (kind, host, executor_id, actor, json.dumps(detail, ensure_ascii=False)))


# --------------------------------------------------------------------------
# invitations
# --------------------------------------------------------------------------

def create_invitation(db, *, mesh: str, host: str, created_by: str,
                      agents: Optional[list] = None,
                      ttl_s: float = INVITATION_TTL_DEFAULT_S) -> dict:
    """Émet un code d'enrôlement. Rend `{"code", "expires_ts", …}` : le code
    n'est montré qu'ici, seul son SHA-256 est gardé.

    L'habilitation de `created_by` (humain, jamais un agent) est contrôlée
    par l'appelant (`host_cli`), qui a le canon."""
    if not mesh or not host or not NAME_RE.match(host):
        raise ValueError("mesh et hôte requis")
    if not created_by.startswith("human:"):
        raise ValueError("seul un humain (human:<id>) émet un code d'enrôlement")
    ttl_s = float(ttl_s)
    if not (0 < ttl_s <= INVITATION_TTL_MAX_S):
        raise ValueError("durée du code : de 1 s à %d min" % (INVITATION_TTL_MAX_S // 60))
    if agents is not None:
        agents = sorted(set(agents))
        bad = [a for a in agents if not NAME_RE.match(a)]
        if bad or not agents:
            raise ValueError("liste d'agents invalide : %s" % (", ".join(bad) or "vide"))
    code = new_code()
    with db.transaction() as tx:
        rows = tx.query(
            "INSERT INTO executor_invitations (code_sha256, mesh, host, agents_allowlist, "
            "created_by, expires_at) VALUES (%s, %s, %s, %s::jsonb, %s, "
            "now() + make_interval(secs => %s)) "
            "RETURNING extract(epoch from expires_at)::float8 AS expires_ts",
            (sha256_hex(normalize_code(code)), mesh, host, _jsonb(agents), created_by, ttl_s))
        _event(tx, "invitation", host, created_by, mesh=mesh, agents=agents,
               ttl_s=ttl_s)
    return {"code": code, "mesh": mesh, "host": host, "agents": agents,
            "expires_ts": rows[0]["expires_ts"], "created_by": created_by}


# --------------------------------------------------------------------------
# fournisseur d'identité
# --------------------------------------------------------------------------

_PRINCIPAL_SQL = """
SELECT t.kind, t.executor_id, t.agent, t.epoch,
       extract(epoch from t.expires_at)::float8 AS expires_ts,
       (t.expires_at > clock_timestamp()) AS valid,
       e.mesh, e.host, e.agents_allowlist, (e.revoked_at IS NOT NULL) AS revoked,
       r.lease_live, r.lease_ts
  FROM executor_tokens t
  JOIN executors e ON e.id = t.executor_id
  LEFT JOIN LATERAL (
        SELECT (a.lease_expires_at > clock_timestamp() AND a.host = e.host) AS lease_live,
               extract(epoch from a.lease_expires_at)::float8 AS lease_ts
          FROM agent_registry a
         WHERE t.kind = 'session' AND a.name = t.agent AND a.lease_epoch = t.epoch
           AND starts_with(a.lease_owner, 'exec:' || t.executor_id || ':')
  ) r ON true
 WHERE t.token_sha256 = %s
"""


class DbIdentityProvider(IdentityProvider):
    """`IdentityProvider` sur la base du serveur du mesh.

    `mesh` : le nom du mesh que sert ce serveur ; un code émis pour un autre
    mesh est refusé à l'enrôlement (None : pas de contrôle, banc d'essai).
    `clock` : horloge des assertions (injectable pour les tests)."""

    def __init__(self, db, *, mesh: Optional[str] = None,
                 clock: Callable[[], float] = time.time,
                 cache_s: float = EXECUTOR_STATE_CACHE_S):
        self.db = db
        self.mesh = mesh
        self._clock = clock
        self._cache_s = min(float(cache_s), float(EXECUTOR_STATE_CACHE_S))
        self._cache: dict[str, tuple[float, Principal]] = {}
        self._lock = threading.Lock()

    # -- vérification -------------------------------------------------------
    def verify(self, token: str) -> Principal:
        return self.verify_kind(token, None)

    def verify_kind(self, token: str, kind: Optional[str]) -> Principal:
        if not isinstance(token, str) or not _TOKEN_RE.match(token):
            raise AuthError("token_invalid", "jeton mal formé")
        token_kind = "executor" if token.startswith(ACCESS_TOKEN_PREFIX) else "session"
        if kind is not None and kind != token_kind:
            raise AuthError("token_invalid", "jeton %s attendu" % (
                "d'accès" if kind == "executor" else "de session"))
        digest = sha256_hex(token)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(digest)
            if hit is not None and now - hit[0] < self._cache_s:
                principal = hit[1]
                if principal.expires_ts > self._clock():
                    return principal
        rows = self.db.query(_PRINCIPAL_SQL, (digest,))
        if not rows:
            raise AuthError("token_invalid", "jeton inconnu")
        row = rows[0]
        if row["revoked"]:
            raise AuthError("executor_revoked", "exécuteur révoqué")
        if not row["valid"]:
            raise AuthError("token_expired", "jeton échu")
        if row["kind"] == "session":
            if not row.get("lease_live"):
                # le bail est perdu, relâché ou son epoch a changé : le jeton
                # de session meurt avec lui
                raise AuthError("token_expired", "bail du jeton de session terminé")
            expires = min(float(row["expires_ts"]), float(row["lease_ts"]))
        else:
            expires = float(row["expires_ts"])
        principal = Principal(
            kind=row["kind"], executor_id=row["executor_id"], mesh=row["mesh"],
            host=row["host"], agent=row.get("agent"),
            epoch=int(row["epoch"]) if row.get("epoch") is not None else None,
            agents_allowlist=_allowlist(row.get("agents_allowlist")), expires_ts=expires)
        with self._lock:
            if len(self._cache) > 4096:
                self._cache.clear()
            self._cache[digest] = (now, principal)
        return principal

    def forget(self, executor_id: Optional[str] = None) -> None:
        """Vide le cache (tout, ou les jetons d'un exécuteur)."""
        with self._lock:
            if executor_id is None:
                self._cache.clear()
            else:
                for key in [k for k, (_, p) in self._cache.items()
                            if p.executor_id == executor_id]:
                    del self._cache[key]

    # -- enrôlement ---------------------------------------------------------
    def enroll(self, request: Mapping[str, Any], *, server_url: str) -> dict:
        if not isinstance(request, Mapping) or request.get("schema") != C.SCHEMA_ENROLL:
            raise AuthError("token_invalid", "schéma %s attendu" % C.SCHEMA_ENROLL)
        code = request.get("code")
        if not isinstance(code, str):
            raise AuthError("token_invalid", "code manquant")
        code = normalize_code(code)
        if not _CODE_RE.match(code):
            raise AuthError("token_invalid", "code d'enrôlement mal formé")
        try:
            public_key = jose.public_jwk(request.get("public_key"))
            message = jose.enroll_proof_message(code, public_key, server_url)
            proof_ok = jose.verify_b64(public_key, message, request.get("proof") or "")
            thumb = jose.thumbprint(public_key)
        except jose.JoseError as exc:
            raise AuthError("token_invalid", "clé publique ou preuve illisible : %s" % exc)
        if not proof_ok:
            raise AuthError("token_invalid", "preuve de possession de la clé fausse")
        code_hash = sha256_hex(code)
        device_key, attestation = self._attestation(
            request.get("device_attestation"), server_url=server_url, code_hash=code_hash,
            thumb=thumb)
        label = request.get("label") or ""
        if not isinstance(label, str) or len(label) > 200:
            raise AuthError("token_invalid", "libellé : texte de 200 caractères au plus")
        executor_id = secrets.token_hex(8)
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT mesh, host, agents_allowlist, created_by, "
                "(consumed_at IS NULL AND cancelled_at IS NULL "
                " AND expires_at > clock_timestamp()) AS usable "
                "FROM executor_invitations WHERE code_sha256 = %s FOR UPDATE", (code_hash,))
            if not rows or not rows[0]["usable"]:
                raise AuthError("token_invalid", "code d'enrôlement inconnu, échu ou déjà utilisé")
            inv = rows[0]
            if self.mesh is not None and inv["mesh"] != self.mesh:
                raise AuthError("token_invalid", "code émis pour un autre mesh")
            known = tx.query("SELECT id FROM executors WHERE thumbprint = %s", (thumb,))
            if known:
                raise AuthError("token_invalid", "clé déjà enrôlée : une clé, un exécuteur")
            tx.query(
                "INSERT INTO executors (id, mesh, host, public_key, thumbprint, "
                "agents_allowlist, label, device_key_sha256, device_attestation, enrolled_by) "
                "VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, %s::jsonb, %s) "
                "RETURNING id",
                (executor_id, inv["mesh"], inv["host"], json.dumps(public_key), thumb,
                 _jsonb(None if inv.get("agents_allowlist") is None
                        else list(_allowlist(inv["agents_allowlist"]))),
                 label, device_key, _jsonb(attestation), inv["created_by"]))
            tx.query("UPDATE executor_invitations SET consumed_at = now(), executor_id = %s "
                     "WHERE code_sha256 = %s RETURNING code_sha256", (executor_id, code_hash))
            _event(tx, "enrolled", inv["host"], inv["created_by"], executor_id,
                   thumbprint=thumb, label=label, device_key_sha256=device_key)
            now_rows = tx.query("SELECT extract(epoch from clock_timestamp())::float8 AS ts")
        return {"executor_id": executor_id, "mesh": inv["mesh"], "host": inv["host"],
                "server_time": now_rows[0]["ts"]}

    @staticmethod
    def _attestation(raw: Any, *, server_url: str, code_hash: str,
                     thumb: str) -> tuple[Optional[str], Optional[dict]]:
        """Liaison facultative à la clé d'appareil Nexlink. Absente : rien.
        Présente mais fausse : l'enrôlement est refusé (elle ne s'invente pas)."""
        if raw is None:
            return None, None
        if not isinstance(raw, Mapping) or raw.get("schema") != jose.SCHEMA_ATTESTATION:
            raise AuthError("token_invalid", "device_attestation : schéma %s attendu"
                            % jose.SCHEMA_ATTESTATION)
        try:
            spki = jose.unb64u(raw.get("device_public_key") or "")
            point = jose.point_from_spki(spki)
            transcript = jose.attestation_transcript(
                server_url=server_url, code_sha256=code_hash, executor_thumbprint=thumb)
            ok = jose.verify_b64(point, transcript, raw.get("signature") or "")
        except jose.JoseError as exc:
            raise AuthError("token_invalid", "device_attestation illisible : %s" % exc)
        if not ok:
            raise AuthError("token_invalid", "device_attestation : signature fausse")
        return (hashlib.sha256(spki).hexdigest(),
                {"schema": jose.SCHEMA_ATTESTATION, "device_public_key": raw["device_public_key"],
                 "signature": raw["signature"]})

    # -- jetons -------------------------------------------------------------
    def issue_access_token(self, assertion: str, *, server_url: str) -> IssuedToken:
        try:
            header, payload, signing_input, sig = jose.jws_parse(assertion)
        except jose.JoseError as exc:
            raise AuthError("token_invalid", str(exc))
        if header.get("alg") != "ES256" or header.get("typ", "JWT") != "JWT" \
                or not isinstance(header.get("kid"), str):
            raise AuthError("token_invalid", "en-tête ES256/JWT avec kid attendu")
        iss = payload.get("iss")
        if not isinstance(iss, str) or not _EXECUTOR_ID_RE.match(iss):
            raise AuthError("token_invalid", "iss : identifiant d'exécuteur attendu")
        rows = self.db.query("SELECT public_key, thumbprint, (revoked_at IS NOT NULL) AS revoked "
                             "FROM executors WHERE id = %s", (iss,))
        if not rows:
            raise AuthError("token_invalid", "exécuteur inconnu")
        row = rows[0]
        if row["revoked"]:
            raise AuthError("executor_revoked", "exécuteur révoqué")
        key = row["public_key"] if isinstance(row["public_key"], dict) \
            else json.loads(row["public_key"])
        if header["kid"] != row["thumbprint"] or not jose.verify_raw(
                jose.point_from_jwk(key), signing_input, sig):
            raise AuthError("token_invalid", "signature de l'assertion fausse")
        try:
            aud_ok = payload.get("aud") == jose.normalize_server_url(server_url)
        except jose.JoseError:
            aud_ok = False
        if not aud_ok:
            raise AuthError("token_invalid", "aud : ce n'est pas ce serveur")
        iat, exp, jti = payload.get("iat"), payload.get("exp"), payload.get("jti")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (iat, exp)):
            raise AuthError("token_invalid", "iat et exp entiers attendus")
        now = self._clock()
        if exp <= now:
            raise AuthError("token_expired", "assertion échue")
        if exp - iat > ASSERTION_MAX_TTL_S or exp < iat or iat > now + CLOCK_SKEW_S \
                or exp > now + ASSERTION_MAX_TTL_S + CLOCK_SKEW_S:
            raise AuthError("token_invalid", "assertion : exp ≤ iat + %d s et horloge "
                            "cohérente attendus" % ASSERTION_MAX_TTL_S)
        if not isinstance(jti, str) or not _JTI_RE.match(jti):
            raise AuthError("token_invalid", "jti attendu")
        token = new_token(ACCESS_TOKEN_PREFIX)
        with self.db.transaction() as tx:
            state = tx.query("SELECT (revoked_at IS NOT NULL) AS revoked FROM executors "
                             "WHERE id = %s FOR UPDATE", (iss,))
            if not state or state[0]["revoked"]:
                raise AuthError("executor_revoked", "exécuteur révoqué")
            tx.query("DELETE FROM executor_assertion_jti WHERE executor_id = %s "
                     "AND expires_at < clock_timestamp() RETURNING jti", (iss,))
            fresh = tx.query(
                "INSERT INTO executor_assertion_jti (executor_id, jti, expires_at) "
                "VALUES (%s, %s, to_timestamp(%s) + make_interval(secs => %s)) "
                "ON CONFLICT DO NOTHING RETURNING jti", (iss, jti, exp, CLOCK_SKEW_S))
            if not fresh:
                raise AuthError("token_invalid", "assertion déjà utilisée (jti)")
            tx.query("DELETE FROM executor_tokens WHERE executor_id = %s "
                     "AND expires_at < clock_timestamp() RETURNING kind", (iss,))
            issued = tx.query(
                "INSERT INTO executor_tokens (token_sha256, kind, executor_id, expires_at) "
                "VALUES (%s, 'executor', %s, now() + make_interval(secs => %s)) "
                "RETURNING extract(epoch from expires_at)::float8 AS expires_ts",
                (sha256_hex(token), iss, ACCESS_TOKEN_TTL_S))
            tx.query("UPDATE executors SET last_seen_at = now() WHERE id = %s RETURNING id",
                     (iss,))
        return IssuedToken(token=token, expires_ts=issued[0]["expires_ts"], kind="executor")

    def issue_session_token(self, principal: Principal, fence: C.Fence) -> IssuedToken:
        if principal.kind != "executor":
            raise AuthError("token_invalid", "un jeton d'accès d'exécuteur est requis")
        if not fence.owner.startswith("exec:%s:" % principal.executor_id):
            raise ScopeError("forbidden_scope", "owner d'un autre exécuteur")
        if principal.agents_allowlist is not None \
                and fence.agent not in principal.agents_allowlist:
            raise ScopeError("forbidden_scope", "agent hors de la liste de l'invitation")
        token = new_token(SESSION_TOKEN_PREFIX)
        with self.db.transaction() as tx:
            state = tx.query("SELECT (revoked_at IS NOT NULL) AS revoked FROM executors "
                             "WHERE id = %s", (principal.executor_id,))
            if not state or state[0]["revoked"]:
                raise AuthError("executor_revoked", "exécuteur révoqué")
            lease = tx.query(
                "SELECT extract(epoch from lease_expires_at)::float8 AS lease_ts "
                "FROM agent_registry WHERE name = %s AND lease_owner = %s AND lease_epoch = %s "
                "AND lease_expires_at > clock_timestamp() AND host = %s",
                (fence.agent, fence.owner, fence.epoch, principal.host))
            if not lease:
                raise ScopeError("forbidden_scope", "bail perdu ou hors de l'hôte de l'exécuteur")
            tx.query(
                "INSERT INTO executor_tokens (token_sha256, kind, executor_id, agent, epoch, "
                "expires_at) VALUES (%s, 'session', %s, %s, %s, "
                "now() + make_interval(secs => %s)) RETURNING kind",
                (sha256_hex(token), principal.executor_id, fence.agent, fence.epoch,
                 SESSION_TOKEN_MAX_S))
        # échéance annoncée : celle du bail à l'émission ; le jeton vit tant
        # que le bail (agent, epoch) vit, renouvellements compris
        return IssuedToken(token=token, expires_ts=lease[0]["lease_ts"], kind="session")

    # -- révocation ---------------------------------------------------------
    def revoke(self, executor_id: str, *, by: str, why: str) -> int:
        with self.db.transaction() as tx:
            released = _revoke_in(tx, executor_id, by=by, why=why)
        self.forget(executor_id)
        for provider in list(_PROVIDERS.values()):
            provider.forget(executor_id)
        return len(released)


def _revoke_in(tx, executor_id: str, *, by: str, why: str) -> list:
    """Dans la transaction `tx` : exécuteur révoqué, jetons effacés, baux
    relâchés (epoch + 1 : le courrier réservé est re-livré comme à
    l'échéance). Rend les noms des agents relâchés."""
    rows = tx.query(
        "UPDATE executors SET revoked_at = coalesce(revoked_at, now()), "
        "revoked_by = coalesce(revoked_by, %s), revoked_why = coalesce(revoked_why, %s) "
        "WHERE id = %s RETURNING host", (by, why, executor_id))
    if not rows:
        raise LookupError("exécuteur %s inconnu" % executor_id)
    tx.query("DELETE FROM executor_tokens WHERE executor_id = %s RETURNING kind",
             (executor_id,))
    released = tx.query(
        "UPDATE agent_registry SET lease_owner = NULL, lease_expires_at = NULL, "
        "lease_epoch = lease_epoch + 1, "
        "status = CASE WHEN status = 'running' THEN 'idle' ELSE status END, "
        "updated_at = now() "
        "WHERE starts_with(lease_owner, 'exec:' || %s || ':') RETURNING name",
        (executor_id,))
    names = sorted(r["name"] for r in released)
    _event(tx, "revoked", rows[0]["host"], by, executor_id, why=why, released=names)
    return names


def revoke_host(db, host: str, *, by: str, why: str,
                executor_id: Optional[str] = None) -> dict:
    """Révoque les exécuteurs de `host` (ou le seul `executor_id`), relâche
    leurs baux et annule les codes en attente de l'hôte. Une transaction."""
    with db.transaction() as tx:
        sql = "SELECT id FROM executors WHERE host = %s AND revoked_at IS NULL"
        params: tuple = (host,)
        if executor_id is not None:
            sql += " AND id = %s"
            params += (executor_id,)
        ids = [r["id"] for r in tx.query(sql + " ORDER BY id FOR UPDATE", params)]
        released: list = []
        for ident in ids:
            released += _revoke_in(tx, ident, by=by, why=why)
        cancelled = []
        if executor_id is None:
            cancelled = tx.query(
                "UPDATE executor_invitations SET cancelled_at = now() WHERE host = %s "
                "AND consumed_at IS NULL AND cancelled_at IS NULL RETURNING code_sha256",
                (host,))
            if cancelled:
                _event(tx, "invitations_cancelled", host, by, count=len(cancelled), why=why)
    for provider in list(_PROVIDERS.values()):
        provider.forget()
    return {"host": host, "executors": ids, "released": sorted(released),
            "invitations_cancelled": len(cancelled)}


# --------------------------------------------------------------------------
# `verify_token` : l'interface de L108 et L111
# --------------------------------------------------------------------------

_PROVIDERS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_PROVIDERS_LOCK = threading.Lock()


def provider_for(db, *, mesh: Optional[str] = None) -> DbIdentityProvider:
    """Le fournisseur (et son cache ≤ 5 s) attaché à cette connexion."""
    with _PROVIDERS_LOCK:
        provider = _PROVIDERS.get(db)
        if provider is None:
            provider = DbIdentityProvider(db, mesh=mesh)
            _PROVIDERS[db] = provider
        return provider


def verify_token(db, token: str, *, kind: Optional[str] = None) -> Principal:
    """Vérifie un jeton d'exécuteur et rend son `Principal`.

    `kind` : "executor" (jeton d'accès `amx1.`, routes `/op`, `/host`,
    `/events`, `/session-token`…), "session" (jeton de session `ams1.`,
    routes `/session/op` et relais de modèle) ou None (l'un ou l'autre).
    Lève `AuthError` : `token_invalid` (mal formé, inconnu, mauvaise sorte),
    `token_expired` (échu ; pour un jeton de session, aussi bail perdu ou
    epoch changé), `executor_revoked`. L'état est relu au plus toutes les
    5 s ; une révocation faite par ce processus vide le cache aussitôt."""
    if kind not in (None, "executor", "session"):
        raise ValueError("kind : executor, session ou None")
    return provider_for(db).verify_kind(token, kind)


# --------------------------------------------------------------------------
# lecture (ameesh host list | show)
# --------------------------------------------------------------------------

_EXECUTOR_COLUMNS = """
       id, mesh, host, thumbprint, label, agents_allowlist, enrolled_by,
       device_key_sha256,
       extract(epoch from enrolled_at)::float8 AS enrolled_ts,
       extract(epoch from last_seen_at)::float8 AS last_seen_ts,
       extract(epoch from revoked_at)::float8 AS revoked_ts,
       revoked_by, revoked_why
"""


def list_executors(db, host: Optional[str] = None) -> list[dict]:
    sql = "SELECT %s FROM executors" % _EXECUTOR_COLUMNS
    params: tuple = ()
    if host is not None:
        sql += " WHERE host = %s"
        params = (host,)
    rows = db.query(sql + " ORDER BY host, enrolled_at", params)
    for row in rows:
        row["agents_allowlist"] = (list(_allowlist(row["agents_allowlist"]))
                                   if row.get("agents_allowlist") is not None else None)
        row["state"] = "révoqué" if row.get("revoked_ts") else "actif"
    return rows


def pending_invitations(db, host: Optional[str] = None) -> list[dict]:
    sql = ("SELECT mesh, host, agents_allowlist, created_by, "
           "extract(epoch from created_at)::float8 AS created_ts, "
           "extract(epoch from expires_at)::float8 AS expires_ts "
           "FROM executor_invitations WHERE consumed_at IS NULL AND cancelled_at IS NULL "
           "AND expires_at > clock_timestamp()")
    params: tuple = ()
    if host is not None:
        sql += " AND host = %s"
        params = (host,)
    rows = db.query(sql + " ORDER BY created_at", params)
    for row in rows:
        row["agents_allowlist"] = (list(_allowlist(row["agents_allowlist"]))
                                   if row.get("agents_allowlist") is not None else None)
    return rows


def host_leases(db, executor_ids: list) -> list[dict]:
    out = []
    for ident in executor_ids:
        out += db.query(
            "SELECT name, lease_owner, lease_epoch, status, "
            "extract(epoch from lease_expires_at)::float8 AS lease_expires_ts "
            "FROM agent_registry WHERE starts_with(lease_owner, 'exec:' || %s || ':') "
            "ORDER BY name", (ident,))
    return out


def events(db, host: str, limit: int = 20) -> list[dict]:
    rows = db.query(
        "SELECT id, kind, executor_id, actor, detail, "
        "extract(epoch from at)::float8 AS ts FROM executor_events WHERE host = %s "
        "ORDER BY id DESC LIMIT %s", (host, int(limit)))
    return list(reversed(rows))
